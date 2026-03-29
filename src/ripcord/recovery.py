from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .adapters import SessionDescriptor, iter_sessions
from .homes import resolve_homes
from .models import ArtifactRef, CandidateSession, TranscriptHints
from .utils import truncate_text


MAX_RECENT_USER_MESSAGES = 5
MAX_RECENT_ASSISTANT_MESSAGES = 5
MAX_RECENT_TOOL_USES = 8


@dataclass(slots=True)
class RecoverySelection:
    status: str
    workspace_root: str
    warnings: list[str] = field(default_factory=list)
    candidates: list[CandidateSession] = field(default_factory=list)
    selected: CandidateSession | None = None


class RipcordRecovery:
    def detect_homes(self, workspace_root: str | None = None) -> dict[str, Any]:
        return resolve_homes(workspace_root).to_dict()

    def discover(
        self,
        workspace_root: str | None = None,
        source_provider: str | None = None,
        host_provider: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        selection = self.select_session(
            workspace_root=workspace_root,
            source_provider=source_provider,
            host_provider=host_provider,
            limit=limit,
        )
        if selection.status == "not_found":
            return {
                "status": "not_found",
                "workspace_root": selection.workspace_root,
                "warnings": selection.warnings,
            }
        if selection.status == "needs_selection":
            return {
                "status": "needs_selection",
                "workspace_root": selection.workspace_root,
                "candidates": [self.session_summary(candidate) for candidate in selection.candidates],
                "warnings": selection.warnings,
            }

        selected = selection.selected
        assert selected is not None
        return {
            "status": "ready",
            "workspace_root": selection.workspace_root,
            "session": self.session_summary(selected),
            "warnings": selection.warnings + list(selected.warnings),
        }

    def recover(
        self,
        workspace_root: str | None = None,
        source_provider: str | None = None,
        session_id: str | None = None,
        host_provider: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        selection = self.select_session(
            workspace_root=workspace_root,
            source_provider=source_provider,
            session_id=session_id,
            host_provider=host_provider,
            limit=limit,
        )
        if selection.status == "not_found":
            return {
                "status": "not_found",
                "workspace_root": selection.workspace_root,
                "warnings": selection.warnings,
            }
        if selection.status == "needs_selection":
            return {
                "status": "needs_selection",
                "workspace_root": selection.workspace_root,
                "candidates": [self.session_summary(candidate) for candidate in selection.candidates],
                "warnings": selection.warnings,
            }

        selected = selection.selected
        assert selected is not None
        return self._build_recovery_response(selected, selection.warnings)

    def select_session(
        self,
        workspace_root: str | None = None,
        source_provider: str | None = None,
        session_id: str | None = None,
        host_provider: str | None = None,
        limit: int = 5,
    ) -> RecoverySelection:
        homes = resolve_homes(workspace_root)
        warnings = list(homes.warnings)
        target_workspace = homes.workspace_root
        providers = self._resolve_provider_search_order(source_provider=source_provider, host_provider=host_provider)

        descriptors: list[SessionDescriptor] = []
        for provider in providers:
            home_status = getattr(homes, f"{provider}_home")
            if not home_status.valid:
                warnings.extend(home_status.warnings)
                continue
            descriptors.extend(
                list(
                    iter_sessions(
                        provider,
                        Path(home_status.path),
                        workspace_root=target_workspace,
                        session_id=session_id,
                    )
                )
            )

        preferred_source = providers[0] if providers else None
        if session_id:
            for descriptor in descriptors:
                if descriptor.session_id == session_id:
                    return RecoverySelection(
                        status="ready",
                        workspace_root=target_workspace,
                        warnings=warnings,
                        selected=descriptor.to_candidate(self._score_descriptor(descriptor, preferred_source)),
                    )
            return RecoverySelection(
                status="not_found",
                workspace_root=target_workspace,
                warnings=warnings + [f"session {session_id} was not found"],
            )

        ranked = self._rank_descriptors(descriptors, preferred_source=preferred_source)
        if not ranked:
            return RecoverySelection(
                status="not_found",
                workspace_root=target_workspace,
                warnings=warnings + ["no matching sessions found"],
            )

        exact_matches = [candidate for candidate in ranked if candidate.exact_workspace_match]
        if len(exact_matches) == 1:
            return RecoverySelection(
                status="ready",
                workspace_root=target_workspace,
                warnings=warnings,
                selected=exact_matches[0],
            )

        return RecoverySelection(
            status="needs_selection",
            workspace_root=target_workspace,
            warnings=warnings,
            candidates=ranked[: max(limit, 1)],
        )

    def session_summary(self, candidate: CandidateSession) -> dict[str, Any]:
        summary = candidate.to_dict()
        summary.pop("artifact_refs", None)
        return summary

    def primary_artifact(self, artifact_refs: list[ArtifactRef]) -> ArtifactRef | None:
        return next((ref for ref in artifact_refs if ref.is_primary), artifact_refs[0] if artifact_refs else None)

    def build_artifact_metadata(self, artifact_ref: ArtifactRef | None, line_count: int | None = None) -> dict[str, Any] | None:
        if artifact_ref is None:
            return None
        path = Path(artifact_ref.path)
        exists = path.exists()
        metadata: dict[str, Any] = {
            "path": artifact_ref.path,
            "exists": exists,
            "is_dir": path.is_dir() if exists else False,
            "size_bytes": path.stat().st_size if exists else None,
        }
        if exists and path.is_file():
            metadata["line_count"] = line_count if line_count is not None else self._count_file_lines(path)
        return metadata

    def build_transcript_index(self, artifact_ref: ArtifactRef | None) -> dict[str, Any] | None:
        if artifact_ref is None or artifact_ref.kind != "transcript":
            return None
        path = Path(artifact_ref.path)
        if not path.exists() or path.is_dir():
            return None

        transcript_index: dict[str, Any] = {
            "line_count": 0,
            "initial_user_prompt": None,
            "user_messages": [],
            "assistant_messages": [],
            "tool_uses": [],
        }
        for line_number, record in self._iter_jsonl_records_with_lines(path):
            transcript_index["line_count"] = line_number

            user_body = self._extract_record_body(artifact_ref.provider, record, role="user")
            if user_body:
                user_entry = self._build_message_entry(line_number, user_body)
                if self._append_unique_message(transcript_index["user_messages"], user_entry):
                    if transcript_index["initial_user_prompt"] is None:
                        text, truncated = truncate_text(user_body.strip(), 4000)
                        transcript_index["initial_user_prompt"] = {
                            "line": line_number,
                            "preview": user_entry["preview"],
                            "text": text,
                            "truncated": truncated,
                            "window": self._build_window(line_number, before=10, line_count=80, max_chars=8000),
                        }

            assistant_body = self._extract_record_body(artifact_ref.provider, record, role="assistant")
            if assistant_body:
                assistant_entry = self._build_message_entry(line_number, assistant_body)
                self._append_unique_message(transcript_index["assistant_messages"], assistant_entry)

            for tool_use in self._extract_tool_uses(artifact_ref.provider, record):
                transcript_index["tool_uses"].append(
                    {
                        "line": line_number,
                        "name": tool_use["name"],
                        "arguments_preview": tool_use["arguments_preview"],
                        "call_id": tool_use.get("call_id"),
                        "window": self._build_window(line_number, before=8, line_count=40, max_chars=6000),
                    }
                )

        return transcript_index

    def build_transcript_hints(self, transcript_index: dict[str, Any] | None) -> TranscriptHints | None:
        if transcript_index is None or transcript_index["line_count"] == 0:
            return None
        user_messages = transcript_index["user_messages"]
        assistant_messages = transcript_index["assistant_messages"]
        first_user = user_messages[0] if user_messages else None
        last_user = user_messages[-1] if user_messages else None
        last_assistant = assistant_messages[-1] if assistant_messages else None
        return TranscriptHints(
            total_lines=transcript_index["line_count"],
            first_user_line=first_user["line"] if first_user else None,
            first_user_preview=first_user["preview"] if first_user else None,
            last_user_line=last_user["line"] if last_user else None,
            last_user_preview=last_user["preview"] if last_user else None,
            last_assistant_line=last_assistant["line"] if last_assistant else None,
            last_assistant_preview=last_assistant["preview"] if last_assistant else None,
        )

    def _build_recovery_response(self, candidate: CandidateSession, warnings: list[str]) -> dict[str, Any]:
        artifact_refs = list(candidate.artifact_refs)
        primary = self.primary_artifact(artifact_refs)
        transcript_index = self.build_transcript_index(primary)
        transcript_hints = self.build_transcript_hints(transcript_index)
        transcript_metadata = self.build_artifact_metadata(
            primary,
            line_count=transcript_index["line_count"] if transcript_index is not None else None,
        )
        initial_user_prompt = transcript_index["initial_user_prompt"] if transcript_index is not None else None
        user_messages = transcript_index["user_messages"] if transcript_index is not None else []
        assistant_messages = transcript_index["assistant_messages"] if transcript_index is not None else []
        tool_uses = transcript_index["tool_uses"] if transcript_index is not None else []
        recent_user_messages = user_messages[-MAX_RECENT_USER_MESSAGES:]
        recent_assistant_messages = assistant_messages[-MAX_RECENT_ASSISTANT_MESSAGES:]
        recent_tool_uses = tool_uses[-MAX_RECENT_TOOL_USES:]
        return {
            "status": "ready",
            "source_provider": candidate.provider,
            "session_id": candidate.session_id,
            "workspace_root": candidate.workspace_root,
            "updated_at": candidate.updated_at,
            "preview": candidate.preview,
            "reasons": list(candidate.reasons),
            "git_state": {
                "branch": candidate.git_branch,
                "sha": candidate.git_sha,
            },
            "transcript_path": primary.path if primary is not None else None,
            "transcript_metadata": transcript_metadata,
            "transcript_hints": transcript_hints.to_dict() if transcript_hints is not None else None,
            "initial_user_prompt": initial_user_prompt,
            "recent_user_messages": recent_user_messages,
            "recent_assistant_messages": recent_assistant_messages,
            "recent_tool_uses": recent_tool_uses,
            "truncation": {
                "initial_user_prompt": bool(initial_user_prompt and initial_user_prompt["truncated"]),
                "recent_user_messages": len(user_messages) > len(recent_user_messages),
                "recent_assistant_messages": len(assistant_messages) > len(recent_assistant_messages),
                "recent_tool_uses": len(tool_uses) > len(recent_tool_uses),
            },
            "warnings": warnings + list(candidate.warnings),
        }

    def _resolve_provider_search_order(self, source_provider: str | None, host_provider: str | None) -> list[str]:
        if source_provider:
            return [source_provider]
        host = host_provider or os.environ.get("RIPCORD_HOST")
        if host == "codex":
            return ["claude", "codex"]
        if host == "claude":
            return ["codex", "claude"]
        return ["codex", "claude"]

    def _score_descriptor(self, descriptor: SessionDescriptor, preferred_source: str | None) -> int:
        score = 0
        if descriptor.exact_workspace_match:
            score += 1000
        if descriptor.provider == preferred_source:
            score += 100
        score += descriptor.completeness_score
        score += descriptor.enrichment_score
        if descriptor.git_branch:
            score += 10
        if descriptor.git_sha:
            score += 10
        return score

    def _rank_descriptors(self, descriptors: list[SessionDescriptor], preferred_source: str | None) -> list[CandidateSession]:
        ranked: list[tuple[int, SessionDescriptor]] = []
        for descriptor in descriptors:
            ranked.append((self._score_descriptor(descriptor, preferred_source), descriptor))
        ranked.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return [descriptor.to_candidate(score) for score, descriptor in ranked]

    def _iter_jsonl_records_with_lines(self, path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield line_number, value

    def _extract_record_body(self, provider: str, record: dict[str, Any], role: str) -> str | None:
        if provider == "codex":
            return self._extract_codex_record_body(record, role)
        if provider == "claude":
            return self._extract_claude_record_body(record, role)
        return None

    def _extract_codex_record_body(self, record: dict[str, Any], role: str) -> str | None:
        record_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        if record_type == "response_item" and payload.get("type") == "message" and payload.get("role") == role:
            return self._flatten_message_text(payload.get("content"))
        if role == "user" and record_type == "event_msg" and payload.get("type") == "user_message":
            return self._flatten_message_text(payload.get("message"))
        if role == "assistant" and record_type == "event_msg" and payload.get("type") == "agent_message":
            return self._flatten_message_text(payload.get("message"))
        return None

    def _extract_claude_record_body(self, record: dict[str, Any], role: str) -> str | None:
        if record.get("type") != role:
            return None
        return self._flatten_message_text(record.get("message"))

    def _extract_tool_uses(self, provider: str, record: dict[str, Any]) -> list[dict[str, str | None]]:
        if provider == "codex":
            tool_use = self._extract_codex_tool_use(record)
            return [tool_use] if tool_use is not None else []
        if provider == "claude":
            return self._extract_claude_tool_uses(record)
        return []

    def _extract_codex_tool_use(self, record: dict[str, Any]) -> dict[str, str | None] | None:
        if record.get("type") != "response_item":
            return None
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "function_call":
            return None
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            return None
        return {
            "name": name,
            "arguments_preview": self._compact_argument_preview(payload.get("arguments")),
            "call_id": payload.get("call_id") if isinstance(payload.get("call_id"), str) else None,
        }

    def _extract_claude_tool_uses(self, record: dict[str, Any]) -> list[dict[str, str | None]]:
        return self._find_tool_use_blocks(record.get("message"))

    def _find_tool_use_blocks(self, value: Any) -> list[dict[str, str | None]]:
        results: list[dict[str, str | None]] = []
        if isinstance(value, list):
            for item in value:
                results.extend(self._find_tool_use_blocks(item))
            return results
        if isinstance(value, dict):
            block_type = value.get("type")
            name = value.get("name") or value.get("tool_name")
            if block_type in {"tool_use", "tool"} and isinstance(name, str) and name:
                results.append(
                    {
                        "name": name,
                        "arguments_preview": self._compact_argument_preview(
                            value.get("input") or value.get("arguments") or value.get("parameters")
                        ),
                        "call_id": value.get("id") if isinstance(value.get("id"), str) else None,
                    }
                )
            for nested in value.values():
                results.extend(self._find_tool_use_blocks(nested))
        return results

    def _build_message_entry(self, line_number: int, text: str) -> dict[str, Any]:
        return {
            "line": line_number,
            "preview": self._compact_preview(text) or "",
            "window": self._build_window(line_number, before=10, line_count=60, max_chars=6000),
        }

    def _build_window(self, line_number: int, before: int, line_count: int, max_chars: int) -> dict[str, int | str]:
        return {
            "mode": "window",
            "line_start": max(line_number - before, 1),
            "line_count": line_count,
            "max_chars": max_chars,
        }

    def _append_unique_message(self, entries: list[dict[str, Any]], entry: dict[str, Any]) -> bool:
        if entries and entries[-1]["preview"] == entry["preview"] and entry["line"] - entries[-1]["line"] <= 2:
            return False
        entries.append(entry)
        return True

    def _compact_argument_preview(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            compact = " ".join(value.split())
        else:
            compact = json.dumps(value, sort_keys=True, ensure_ascii=True)
        preview, _ = truncate_text(compact, 240)
        return preview

    def _flatten_message_text(self, value: Any) -> str | None:
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                text = self._flatten_message_text(item)
                if text:
                    parts.append(text)
            return "\n".join(parts).strip() or None
        if isinstance(value, dict):
            direct_text = value.get("text")
            if isinstance(direct_text, str) and direct_text.strip():
                return direct_text.strip()
            for key in ("content", "message"):
                nested = value.get(key)
                text = self._flatten_message_text(nested)
                if text:
                    return text
        return None

    def _compact_preview(self, text: str | None) -> str | None:
        if not text:
            return None
        preview = " ".join(text.split())
        preview, _ = truncate_text(preview, 160)
        return preview

    def _count_file_lines(self, path: Path) -> int:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
