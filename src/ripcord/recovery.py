import json
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .homes import resolve_homes
from .models import ArtifactRef, CandidateSession, SessionDescriptor, TranscriptEvent, TranscriptHints
from .providers import get_provider
from .utils import compact_preview, open_text, truncate_text
from .workspace import WORKSPACE_MATCH_REASONS, WORKSPACE_MATCH_SCORES, WorkspaceMatcher


MAX_RECENT_USER_MESSAGES = 5
MAX_RECENT_ASSISTANT_MESSAGES = 5
MAX_RECENT_REASONING = 5
MAX_RECENT_TOOL_USES = 8
INITIAL_PROMPT_TEXT_LIMIT = 4000
MESSAGE_TEXT_LIMIT = 2000
REASONING_TEXT_LIMIT = 1000
SUMMARY_TEXT_LIMIT = 8000
# Codex mirrors each message as a response_item and an event_msg a few lines apart.
DUPLICATE_LINE_WINDOW = 20


@dataclass(slots=True)
class RecoverySelection:
    status: str
    workspace_root: str
    warnings: list[str] = field(default_factory=list)
    candidates: list[CandidateSession] = field(default_factory=list)
    selected: CandidateSession | None = None


@dataclass(slots=True)
class TranscriptIndex:
    line_count: int = 0
    initial_user_prompt: dict[str, Any] | None = None
    first_command_prompt: dict[str, Any] | None = None
    first_user_message: dict[str, Any] | None = None
    compaction_summary: dict[str, Any] | None = None
    user_messages: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_RECENT_USER_MESSAGES))
    assistant_messages: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_RECENT_ASSISTANT_MESSAGES))
    reasoning: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_RECENT_REASONING))
    tool_uses: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_RECENT_TOOL_USES))
    user_message_count: int = 0
    assistant_message_count: int = 0
    reasoning_count: int = 0
    tool_use_count: int = 0

    def add(self, line_number: int, event: TranscriptEvent) -> None:
        if event.kind == "user" and event.text:
            entry = _message_entry(line_number, event.text, MESSAGE_TEXT_LIMIT)
            if not _append_unique(self.user_messages, entry):
                return
            self.user_message_count += 1
            self.first_user_message = self.first_user_message or entry
            prompt = _message_entry(line_number, event.text, INITIAL_PROMPT_TEXT_LIMIT)
            if event.is_command:
                self.first_command_prompt = self.first_command_prompt or prompt
            elif self.initial_user_prompt is None:
                self.initial_user_prompt = prompt
        elif event.kind == "assistant" and event.text:
            if _append_unique(self.assistant_messages, _message_entry(line_number, event.text, MESSAGE_TEXT_LIMIT)):
                self.assistant_message_count += 1
        elif event.kind == "reasoning" and event.text:
            if _append_unique(self.reasoning, _message_entry(line_number, event.text, REASONING_TEXT_LIMIT)):
                self.reasoning_count += 1
        elif event.kind == "summary" and event.text:
            text, truncated = truncate_text(event.text, SUMMARY_TEXT_LIMIT)
            self.compaction_summary = {"line": line_number, "text": text, "truncated": truncated}
        elif event.kind == "tool_use" and event.name:
            self.tool_use_count += 1
            self.tool_uses.append(
                {
                    "line": line_number,
                    "name": event.name,
                    "arguments_preview": _compact_argument_preview(event.arguments),
                    "call_id": event.call_id,
                }
            )

    @property
    def prompt(self) -> dict[str, Any] | None:
        return self.initial_user_prompt or self.first_command_prompt


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
        target_workspace = homes.workspace_root
        providers = self._resolve_provider_search_order(source_provider=source_provider, host_provider=host_provider)

        warnings: list[str] = []
        descriptors: list[SessionDescriptor] = []
        for provider in providers:
            home_status = homes.homes[provider]
            warnings.extend(home_status.warnings)
            if not home_status.valid:
                continue
            descriptors.extend(get_provider(provider).load_sessions(Path(home_status.path), session_id=session_id))

        preferred_source = providers[0] if providers else None
        ranked = self._rank_descriptors(descriptors, preferred_source, WorkspaceMatcher(target_workspace))
        if session_id:
            for candidate in ranked:
                if candidate.session_id == session_id:
                    return RecoverySelection(
                        status="ready",
                        workspace_root=target_workspace,
                        warnings=warnings,
                        selected=candidate,
                    )
            return RecoverySelection(
                status="not_found",
                workspace_root=target_workspace,
                warnings=warnings + [f"session {session_id} was not found"],
            )

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

    def build_transcript_index(self, artifact_ref: ArtifactRef | None) -> TranscriptIndex | None:
        if artifact_ref is None or artifact_ref.kind != "transcript":
            return None
        path = Path(artifact_ref.path)
        if not path.is_file():
            return None

        provider = get_provider(artifact_ref.provider)
        transcript_index = TranscriptIndex()
        for line_number, record in self._iter_jsonl_records_with_lines(path):
            transcript_index.line_count = line_number
            for event in provider.parse_record(record):
                transcript_index.add(line_number, event)
        return transcript_index

    def build_transcript_hints(self, transcript_index: TranscriptIndex | None) -> TranscriptHints | None:
        if transcript_index is None or transcript_index.line_count == 0:
            return None
        first_user = transcript_index.first_user_message
        last_user = transcript_index.user_messages[-1] if transcript_index.user_messages else None
        last_assistant = transcript_index.assistant_messages[-1] if transcript_index.assistant_messages else None
        return TranscriptHints(
            total_lines=transcript_index.line_count,
            first_user_line=first_user["line"] if first_user else None,
            first_user_preview=first_user["preview"] if first_user else None,
            last_user_line=last_user["line"] if last_user else None,
            last_user_preview=last_user["preview"] if last_user else None,
            last_assistant_line=last_assistant["line"] if last_assistant else None,
            last_assistant_preview=last_assistant["preview"] if last_assistant else None,
        )

    def _build_recovery_response(self, candidate: CandidateSession, warnings: list[str]) -> dict[str, Any]:
        primary = self.primary_artifact(list(candidate.artifact_refs))
        transcript_index = self.build_transcript_index(primary) or TranscriptIndex()
        transcript_hints = self.build_transcript_hints(transcript_index)
        transcript_metadata = self.build_artifact_metadata(
            primary,
            line_count=transcript_index.line_count or None,
        )
        initial_user_prompt = transcript_index.prompt
        return {
            "status": "ready",
            "source_provider": candidate.provider,
            "session_id": candidate.session_id,
            "workspace_root": candidate.workspace_root,
            "workspace_match": candidate.workspace_match,
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
            "compaction_summary": transcript_index.compaction_summary,
            "recent_user_messages": list(transcript_index.user_messages),
            "recent_assistant_messages": list(transcript_index.assistant_messages),
            "recent_reasoning": list(transcript_index.reasoning),
            "recent_tool_uses": list(transcript_index.tool_uses),
            "truncation": {
                "initial_user_prompt": bool(initial_user_prompt and initial_user_prompt["truncated"]),
                "recent_user_messages": transcript_index.user_message_count > len(transcript_index.user_messages),
                "recent_assistant_messages": transcript_index.assistant_message_count > len(transcript_index.assistant_messages),
                "recent_tool_uses": transcript_index.tool_use_count > len(transcript_index.tool_uses),
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

    def _score_descriptor(self, descriptor: SessionDescriptor, preferred_source: str | None, workspace_match: str) -> int:
        score = WORKSPACE_MATCH_SCORES[workspace_match]
        if descriptor.provider == preferred_source:
            score += 100
        score += descriptor.completeness_score
        score += descriptor.enrichment_score
        if descriptor.git_branch:
            score += 10
        if descriptor.git_sha:
            score += 10
        return score

    def _rank_descriptors(
        self,
        descriptors: list[SessionDescriptor],
        preferred_source: str | None,
        matcher: WorkspaceMatcher,
    ) -> list[CandidateSession]:
        candidates: list[CandidateSession] = []
        for descriptor in descriptors:
            workspace_match = matcher.relation(descriptor.workspace_root)
            score = self._score_descriptor(descriptor, preferred_source, workspace_match)
            candidates.append(descriptor.to_candidate(score, workspace_match, WORKSPACE_MATCH_REASONS[workspace_match]))
        candidates.sort(key=lambda candidate: (candidate.score, candidate.updated_at), reverse=True)
        return candidates

    def _iter_jsonl_records_with_lines(self, path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
        with open_text(path) as handle:
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

    def _count_file_lines(self, path: Path) -> int:
        with open_text(path) as handle:
            return sum(1 for _ in handle)


def _message_entry(line_number: int, text: str, limit: int) -> dict[str, Any]:
    bounded, truncated = truncate_text(text, limit)
    return {"line": line_number, "preview": compact_preview(text), "text": bounded, "truncated": truncated}


def _append_unique(entries: deque[dict[str, Any]], entry: dict[str, Any]) -> bool:
    if entries and entries[-1]["text"] == entry["text"] and entry["line"] - entries[-1]["line"] <= DUPLICATE_LINE_WINDOW:
        return False
    entries.append(entry)
    return True


def _compact_argument_preview(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        compact = " ".join(value.split())
    else:
        compact = json.dumps(value, sort_keys=True, ensure_ascii=True)
    preview, _ = truncate_text(compact, 240)
    return preview
