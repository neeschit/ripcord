#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_RECENT_USER_MESSAGES = 5
MAX_RECENT_ASSISTANT_MESSAGES = 5
MAX_RECENT_TOOL_USES = 8


def expand_path(value: str | Path) -> Path:
    return Path(value).expanduser()


def canonicalize_path(value: str | Path | None) -> str | None:
    if value is None:
        return None
    try:
        return str(expand_path(value).resolve())
    except FileNotFoundError:
        return str(expand_path(value).absolute())


def iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def epoch_to_iso(value: int | float | None) -> str:
    if value is None:
        return ""
    if value > 10_000_000_000:
        value = value / 1000
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit <= 3:
        return text[:limit], True
    return text[: limit - 3] + "...", True


@dataclass(slots=True)
class ArtifactRef:
    provider: str
    session_id: str
    kind: str
    path: str
    label: str
    is_primary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CandidateSession:
    provider: str
    session_id: str
    workspace_root: str
    updated_at: str
    git_branch: str | None
    git_sha: str | None
    score: int
    preview: str
    reasons: list[str]
    exact_workspace_match: bool
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SessionDescriptor:
    provider: str
    session_id: str
    workspace_root: str
    updated_at: str
    git_branch: str | None
    git_sha: str | None
    preview: str
    completeness_score: int
    enrichment_score: int
    exact_workspace_match: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifact_refs: list[ArtifactRef] = field(default_factory=list)

    def to_candidate(self, score: int) -> CandidateSession:
        return CandidateSession(
            provider=self.provider,
            session_id=self.session_id,
            workspace_root=self.workspace_root,
            updated_at=self.updated_at,
            git_branch=self.git_branch,
            git_sha=self.git_sha,
            score=score,
            preview=self.preview,
            reasons=list(self.reasons),
            exact_workspace_match=self.exact_workspace_match,
            artifact_refs=list(self.artifact_refs),
            warnings=list(self.warnings),
        )


def resolve_homes(workspace_root: str | None = None) -> dict[str, Any]:
    def validate_home(path: Path, required_markers: list[str], optional_markers: list[str]) -> dict[str, Any]:
        exists = path.exists()
        present_required = [marker for marker in required_markers if (path / marker).exists()]
        present_optional = []
        for marker in optional_markers:
            if "*" in marker:
                if list(path.glob(marker)):
                    present_optional.append(marker)
                continue
            if (path / marker).exists():
                present_optional.append(marker)
        missing_required = [marker for marker in required_markers if marker not in present_required]
        warnings: list[str] = []
        if not exists:
            warnings.append(f"{path} does not exist")
        elif missing_required:
            warnings.append(f"{path} is missing required markers: {', '.join(missing_required)}")
        return {
            "path": canonicalize_path(path) or str(path),
            "exists": exists,
            "valid": exists and not missing_required,
            "markers_present": present_required + present_optional,
            "missing_markers": missing_required,
            "warnings": warnings,
        }

    ripcord_home = expand_path(os.environ.get("RIPCORD_HOME", "~/.ripcord"))
    codex_home = expand_path(os.environ.get("CODEX_HOME", "~/.codex"))
    claude_home = expand_path(os.environ.get("CLAUDE_HOME", "~/.claude"))
    workspace = expand_path(workspace_root or os.getcwd())
    codex_status = validate_home(
        codex_home,
        required_markers=["auth.json", "session_index.jsonl", "sessions"],
        optional_markers=["shell_snapshots", "state_5.sqlite", "state_*.sqlite"],
    )
    claude_status = validate_home(
        claude_home,
        required_markers=["projects"],
        optional_markers=["usage-data/session-meta", "file-history", "shell-snapshots"],
    )
    return {
        "ripcord_home": canonicalize_path(ripcord_home) or str(ripcord_home),
        "codex_home": codex_status,
        "claude_home": claude_status,
        "workspace_root": canonicalize_path(workspace) or str(workspace),
        "warnings": list(codex_status["warnings"]) + list(claude_status["warnings"]),
    }


def workspace_reason(exact_match: bool) -> str:
    return "exact workspace match" if exact_match else "non-exact workspace match"


def read_codex_threads(codex_home: Path, session_id: str | None = None) -> list[dict[str, Any]]:
    database_candidates = sorted(codex_home.glob("state_*.sqlite"))
    if not database_candidates:
        return read_codex_from_index(codex_home)

    database_path = database_candidates[-1]
    query = """
        select id, rollout_path, updated_at, cwd, title, git_branch, git_sha, first_user_message
        from threads
        where archived = 0
    """
    params: tuple[Any, ...] = ()
    if session_id:
        query += " and id = ?"
        params = (session_id,)
    query += " order by updated_at desc"
    try:
        with closing(sqlite3.connect(database_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
    except sqlite3.Error:
        return read_codex_from_index(codex_home)
    return [dict(row) for row in rows]


def read_codex_from_index(codex_home: Path) -> list[dict[str, Any]]:
    index_path = codex_home / "session_index.jsonl"
    if not index_path.exists():
        return []
    rollouts = map_codex_rollouts(codex_home)
    rows: list[dict[str, Any]] = []
    for entry in iter_jsonl(index_path):
        current_session_id = entry.get("id", "")
        rows.append(
            {
                "id": current_session_id,
                "rollout_path": str(rollouts[current_session_id]) if current_session_id in rollouts else "",
                "updated_at": 0,
                "cwd": "",
                "title": entry.get("thread_name", current_session_id),
                "git_branch": None,
                "git_sha": None,
                "first_user_message": entry.get("thread_name", ""),
            }
        )
    return rows


def map_codex_rollouts(codex_home: Path) -> dict[str, Path]:
    results: dict[str, Path] = {}
    for path in codex_home.glob("sessions/**/*.jsonl"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                first_line = handle.readline().strip()
        except OSError:
            continue
        if not first_line:
            continue
        try:
            record = json.loads(first_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload", {})
        if isinstance(payload, dict) and record.get("type") == "session_meta":
            current_session_id = payload.get("id")
            if isinstance(current_session_id, str) and current_session_id:
                results[current_session_id] = path
    return results


def load_codex_sessions(codex_home: Path, workspace_root: str | None = None, session_id: str | None = None) -> list[SessionDescriptor]:
    canonical_workspace = canonicalize_path(workspace_root)
    rows = read_codex_threads(codex_home, session_id=session_id)
    results: list[SessionDescriptor] = []
    for row in rows:
        current_session_id = row["id"]
        if session_id and current_session_id != session_id:
            continue
        cwd = canonicalize_path(row.get("cwd")) or ""
        exact_match = canonical_workspace is not None and cwd == canonical_workspace
        rollout_path = row.get("rollout_path") or ""
        transcript_path = Path(rollout_path) if rollout_path else None
        transcript_exists = transcript_path is not None and transcript_path.exists()
        warnings: list[str] = []
        if not transcript_exists:
            warnings.append(f"missing primary transcript: {rollout_path or '<unknown>'}")
        preview = row.get("first_user_message") or row.get("title") or current_session_id
        preview, _ = truncate_text(preview.strip(), 160)
        artifact_refs = [
            ArtifactRef(
                provider="codex",
                session_id=current_session_id,
                kind="transcript",
                path=str(transcript_path) if transcript_path is not None else "",
                label="Codex rollout transcript",
                is_primary=True,
            )
        ]
        shell_glob = codex_home / "shell_snapshots"
        shell_refs = list(sorted(shell_glob.glob(f"{current_session_id}.*.sh"))) if shell_glob.exists() else []
        for snap in shell_refs:
            artifact_refs.append(
                ArtifactRef(
                    provider="codex",
                    session_id=current_session_id,
                    kind="shell_snapshot",
                    path=str(snap),
                    label=f"Shell snapshot {snap.name}",
                )
            )
        reasons = [workspace_reason(exact_match)]
        if row.get("git_branch"):
            reasons.append("git branch metadata present")
        if row.get("git_sha"):
            reasons.append("git sha metadata present")
        if transcript_exists:
            reasons.append("primary transcript present")
        if shell_refs:
            reasons.append("shell snapshots present")
        results.append(
            SessionDescriptor(
                provider="codex",
                session_id=current_session_id,
                workspace_root=cwd,
                updated_at=epoch_to_iso(row.get("updated_at")),
                git_branch=row.get("git_branch"),
                git_sha=row.get("git_sha"),
                preview=preview,
                completeness_score=25 if transcript_exists else 0,
                enrichment_score=len(shell_refs) * 2 + (2 if row.get("git_branch") else 0) + (2 if row.get("git_sha") else 0),
                exact_workspace_match=exact_match,
                reasons=reasons,
                warnings=warnings,
                artifact_refs=artifact_refs,
            )
        )
    return results


def read_claude_meta(claude_home: Path, session_id: str | None = None) -> dict[str, dict[str, Any]]:
    meta_dir = claude_home / "usage-data" / "session-meta"
    if not meta_dir.exists():
        return {}
    if session_id:
        meta_file = meta_dir / f"{session_id}.json"
        if not meta_file.exists():
            return {}
        try:
            return {session_id: json.loads(meta_file.read_text(encoding="utf-8"))}
        except json.JSONDecodeError:
            return {}
    result: dict[str, dict[str, Any]] = {}
    for meta_file in meta_dir.glob("*.json"):
        try:
            result[meta_file.stem] = json.loads(meta_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
    return result


def read_claude_transcript_metadata(transcript_path: Path) -> tuple[str, str, str, str | None]:
    cwd = ""
    preview = transcript_path.stem
    updated_at = ""
    git_branch = None
    for record in iter_jsonl(transcript_path):
        timestamp = record.get("timestamp")
        if timestamp:
            updated_at = timestamp
        cwd = record.get("cwd", cwd)
        git_branch = record.get("gitBranch", git_branch)
        if record.get("type") == "user":
            message = record.get("message", {})
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                preview = content.strip()
                break
    return cwd, preview, updated_at, git_branch


def load_claude_sessions(claude_home: Path, workspace_root: str | None = None, session_id: str | None = None) -> list[SessionDescriptor]:
    canonical_workspace = canonicalize_path(workspace_root)
    meta_by_id = read_claude_meta(claude_home, session_id=session_id)
    results: list[SessionDescriptor] = []
    for transcript_path in sorted(claude_home.glob("projects/**/*.jsonl")):
        if "/subagents/" in str(transcript_path):
            continue
        current_session_id = transcript_path.stem
        if session_id and current_session_id != session_id:
            continue
        cwd, preview, updated_at, git_branch = read_claude_transcript_metadata(transcript_path)
        exact_match = canonical_workspace is not None and canonicalize_path(cwd) == canonical_workspace
        metadata = meta_by_id.get(current_session_id, {})
        artifact_refs = [
            ArtifactRef(
                provider="claude",
                session_id=current_session_id,
                kind="transcript",
                path=str(transcript_path),
                label="Claude project transcript",
                is_primary=True,
            )
        ]
        meta_path = claude_home / "usage-data" / "session-meta" / f"{current_session_id}.json"
        has_meta = meta_path.exists()
        if has_meta:
            artifact_refs.append(
                ArtifactRef(
                    provider="claude",
                    session_id=current_session_id,
                    kind="metadata",
                    path=str(meta_path),
                    label="Claude session metadata",
                )
            )
        session_dir = transcript_path.with_suffix("")
        subagent_refs = list(sorted((session_dir / "subagents").glob("*.jsonl"))) if session_dir.exists() else []
        for subagent in subagent_refs:
            artifact_refs.append(
                ArtifactRef(
                    provider="claude",
                    session_id=current_session_id,
                    kind="subagent",
                    path=str(subagent),
                    label=f"Subagent transcript {subagent.name}",
                )
            )
        file_history_root = claude_home / "file-history" / current_session_id
        has_file_history = file_history_root.exists()
        if has_file_history:
            artifact_refs.append(
                ArtifactRef(
                    provider="claude",
                    session_id=current_session_id,
                    kind="file_history",
                    path=str(file_history_root),
                    label="Claude file history directory",
                )
            )
        preview, _ = truncate_text(preview, 160)
        reasons = [workspace_reason(exact_match), "primary transcript present"]
        if git_branch:
            reasons.append("git branch metadata present")
        if has_meta:
            reasons.append("session metadata present")
        if subagent_refs:
            reasons.append("subagent transcripts present")
        if has_file_history:
            reasons.append("file history present")
        results.append(
            SessionDescriptor(
                provider="claude",
                session_id=current_session_id,
                workspace_root=canonicalize_path(cwd) or cwd,
                updated_at=updated_at or metadata.get("start_time", ""),
                git_branch=git_branch,
                git_sha=None,
                preview=preview,
                completeness_score=25,
                enrichment_score=(2 if has_meta else 0) + len(subagent_refs) * 2 + (2 if has_file_history else 0),
                exact_workspace_match=exact_match,
                reasons=reasons,
                warnings=[],
                artifact_refs=artifact_refs,
            )
        )
    return results


def iter_sessions(provider: str, home: Path, workspace_root: str | None = None, session_id: str | None = None) -> list[SessionDescriptor]:
    if provider == "codex":
        return load_codex_sessions(home, workspace_root=workspace_root, session_id=session_id)
    if provider == "claude":
        return load_claude_sessions(home, workspace_root=workspace_root, session_id=session_id)
    raise ValueError(f"unsupported provider: {provider}")


class RecoveryEngine:
    def select_session(
        self,
        workspace_root: str | None = None,
        source_provider: str | None = None,
        session_id: str | None = None,
        host_provider: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        homes = resolve_homes(workspace_root)
        warnings = list(homes["warnings"])
        target_workspace = homes["workspace_root"]
        providers = self.resolve_provider_search_order(source_provider=source_provider, host_provider=host_provider)
        descriptors: list[SessionDescriptor] = []
        for provider in providers:
            home_status = homes[f"{provider}_home"]
            if not home_status["valid"]:
                warnings.extend(home_status["warnings"])
                continue
            descriptors.extend(
                iter_sessions(
                    provider,
                    Path(home_status["path"]),
                    workspace_root=target_workspace,
                    session_id=session_id,
                )
            )

        preferred_source = providers[0] if providers else None
        if session_id:
            for descriptor in descriptors:
                if descriptor.session_id == session_id:
                    return {
                        "status": "ready",
                        "workspace_root": target_workspace,
                        "warnings": warnings,
                        "selected": descriptor.to_candidate(self.score_descriptor(descriptor, preferred_source)),
                    }
            return {
                "status": "not_found",
                "workspace_root": target_workspace,
                "warnings": warnings + [f"session {session_id} was not found"],
            }

        ranked = self.rank_descriptors(descriptors, preferred_source)
        if not ranked:
            return {
                "status": "not_found",
                "workspace_root": target_workspace,
                "warnings": warnings + ["no matching sessions found"],
            }
        exact_matches = [candidate for candidate in ranked if candidate.exact_workspace_match]
        if len(exact_matches) == 1:
            return {
                "status": "ready",
                "workspace_root": target_workspace,
                "warnings": warnings,
                "selected": exact_matches[0],
            }
        return {
            "status": "needs_selection",
            "workspace_root": target_workspace,
            "warnings": warnings,
            "candidates": ranked[: max(limit, 1)],
        }

    def discover(self, workspace_root: str, source_provider: str, host_provider: str | None = None, limit: int = 5) -> dict[str, Any]:
        selection = self.select_session(
            workspace_root=workspace_root,
            source_provider=source_provider,
            host_provider=host_provider,
            limit=limit,
        )
        if selection["status"] == "needs_selection":
            return {
                "status": "needs_selection",
                "workspace_root": selection["workspace_root"],
                "candidates": [self.session_summary(candidate) for candidate in selection["candidates"]],
                "warnings": selection["warnings"],
            }
        if selection["status"] == "not_found":
            return selection
        selected = selection["selected"]
        return {
            "status": "ready",
            "workspace_root": selection["workspace_root"],
            "session": self.session_summary(selected),
            "warnings": selection["warnings"] + list(selected.warnings),
        }

    def recover(
        self,
        workspace_root: str,
        source_provider: str,
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
        if selection["status"] == "not_found":
            return selection
        if selection["status"] == "needs_selection":
            return {
                "status": "needs_selection",
                "workspace_root": selection["workspace_root"],
                "candidates": [self.session_summary(candidate) for candidate in selection["candidates"]],
                "warnings": selection["warnings"],
            }
        candidate = selection["selected"]
        primary = next((ref for ref in candidate.artifact_refs if ref.is_primary), candidate.artifact_refs[0] if candidate.artifact_refs else None)
        transcript_index = self.build_transcript_index(primary)
        transcript_hints = self.build_transcript_hints(transcript_index)
        transcript_metadata = self.build_artifact_metadata(primary, transcript_index["line_count"] if transcript_index else None)
        initial_user_prompt = transcript_index["initial_user_prompt"] if transcript_index else None
        user_messages = transcript_index["user_messages"] if transcript_index else []
        assistant_messages = transcript_index["assistant_messages"] if transcript_index else []
        tool_uses = transcript_index["tool_uses"] if transcript_index else []
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
            "git_state": {"branch": candidate.git_branch, "sha": candidate.git_sha},
            "transcript_path": primary.path if primary else None,
            "transcript_metadata": transcript_metadata,
            "transcript_hints": transcript_hints,
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
            "warnings": selection["warnings"] + list(candidate.warnings),
        }

    def session_summary(self, candidate: CandidateSession) -> dict[str, Any]:
        return {
            "provider": candidate.provider,
            "session_id": candidate.session_id,
            "workspace_root": candidate.workspace_root,
            "updated_at": candidate.updated_at,
            "git_branch": candidate.git_branch,
            "git_sha": candidate.git_sha,
            "score": candidate.score,
            "preview": candidate.preview,
            "reasons": list(candidate.reasons),
            "exact_workspace_match": candidate.exact_workspace_match,
            "warnings": list(candidate.warnings),
        }

    def resolve_provider_search_order(self, source_provider: str | None, host_provider: str | None) -> list[str]:
        if source_provider:
            return [source_provider]
        host = host_provider or os.environ.get("RIPCORD_HOST")
        if host == "codex":
            return ["claude", "codex"]
        if host == "claude":
            return ["codex", "claude"]
        return ["codex", "claude"]

    def score_descriptor(self, descriptor: SessionDescriptor, preferred_source: str | None) -> int:
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

    def rank_descriptors(self, descriptors: list[SessionDescriptor], preferred_source: str | None) -> list[CandidateSession]:
        ranked: list[tuple[int, SessionDescriptor]] = []
        for descriptor in descriptors:
            ranked.append((self.score_descriptor(descriptor, preferred_source), descriptor))
        ranked.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return [descriptor.to_candidate(score) for score, descriptor in ranked]

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
            metadata["line_count"] = line_count if line_count is not None else self.count_file_lines(path)
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
        for line_number, record in self.iter_records_with_lines(path):
            transcript_index["line_count"] = line_number
            user_body = self.extract_record_body(artifact_ref.provider, record, "user")
            if user_body:
                user_entry = self.build_message_entry(line_number, user_body)
                if self.append_unique_message(transcript_index["user_messages"], user_entry):
                    if transcript_index["initial_user_prompt"] is None:
                        text, truncated = truncate_text(user_body.strip(), 4000)
                        transcript_index["initial_user_prompt"] = {
                            "line": line_number,
                            "preview": user_entry["preview"],
                            "text": text,
                            "truncated": truncated,
                        }
            assistant_body = self.extract_record_body(artifact_ref.provider, record, "assistant")
            if assistant_body:
                assistant_entry = self.build_message_entry(line_number, assistant_body)
                self.append_unique_message(transcript_index["assistant_messages"], assistant_entry)
            for tool_use in self.extract_tool_uses(artifact_ref.provider, record):
                transcript_index["tool_uses"].append(
                    {
                        "line": line_number,
                        "name": tool_use["name"],
                        "arguments_preview": tool_use["arguments_preview"],
                        "call_id": tool_use.get("call_id"),
                    }
                )
        return transcript_index

    def build_transcript_hints(self, transcript_index: dict[str, Any] | None) -> dict[str, Any] | None:
        if transcript_index is None or transcript_index["line_count"] == 0:
            return None
        user_messages = transcript_index["user_messages"]
        assistant_messages = transcript_index["assistant_messages"]
        first_user = user_messages[0] if user_messages else None
        last_user = user_messages[-1] if user_messages else None
        last_assistant = assistant_messages[-1] if assistant_messages else None
        return {
            "total_lines": transcript_index["line_count"],
            "first_user_line": first_user["line"] if first_user else None,
            "first_user_preview": first_user["preview"] if first_user else None,
            "last_user_line": last_user["line"] if last_user else None,
            "last_user_preview": last_user["preview"] if last_user else None,
            "last_assistant_line": last_assistant["line"] if last_assistant else None,
            "last_assistant_preview": last_assistant["preview"] if last_assistant else None,
        }

    def iter_records_with_lines(self, path: Path):
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

    def extract_record_body(self, provider: str, record: dict[str, Any], role: str) -> str | None:
        if provider == "codex":
            return self.extract_codex_record_body(record, role)
        if provider == "claude":
            return self.extract_claude_record_body(record, role)
        return None

    def extract_codex_record_body(self, record: dict[str, Any], role: str) -> str | None:
        record_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        if record_type == "response_item" and payload.get("type") == "message" and payload.get("role") == role:
            return self.flatten_message_text(payload.get("content"))
        if role == "user" and record_type == "event_msg" and payload.get("type") == "user_message":
            return self.flatten_message_text(payload.get("message"))
        if role == "assistant" and record_type == "event_msg" and payload.get("type") == "agent_message":
            return self.flatten_message_text(payload.get("message"))
        return None

    def extract_claude_record_body(self, record: dict[str, Any], role: str) -> str | None:
        if record.get("type") != role:
            return None
        return self.flatten_message_text(record.get("message"))

    def extract_tool_uses(self, provider: str, record: dict[str, Any]) -> list[dict[str, str | None]]:
        if provider == "codex":
            tool_use = self.extract_codex_tool_use(record)
            return [tool_use] if tool_use is not None else []
        if provider == "claude":
            return self.extract_claude_tool_uses(record)
        return []

    def extract_codex_tool_use(self, record: dict[str, Any]) -> dict[str, str | None] | None:
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
            "arguments_preview": self.compact_argument_preview(payload.get("arguments")),
            "call_id": payload.get("call_id") if isinstance(payload.get("call_id"), str) else None,
        }

    def extract_claude_tool_uses(self, record: dict[str, Any]) -> list[dict[str, str | None]]:
        return self.find_tool_use_blocks(record.get("message"))

    def find_tool_use_blocks(self, value: Any) -> list[dict[str, str | None]]:
        results: list[dict[str, str | None]] = []
        if isinstance(value, list):
            for item in value:
                results.extend(self.find_tool_use_blocks(item))
            return results
        if isinstance(value, dict):
            block_type = value.get("type")
            name = value.get("name") or value.get("tool_name")
            if block_type in {"tool_use", "tool"} and isinstance(name, str) and name:
                results.append(
                    {
                        "name": name,
                        "arguments_preview": self.compact_argument_preview(
                            value.get("input") or value.get("arguments") or value.get("parameters")
                        ),
                        "call_id": value.get("id") if isinstance(value.get("id"), str) else None,
                    }
                )
            for nested in value.values():
                results.extend(self.find_tool_use_blocks(nested))
        return results

    def build_message_entry(self, line_number: int, text: str) -> dict[str, Any]:
        return {"line": line_number, "preview": self.compact_preview(text) or ""}

    def append_unique_message(self, entries: list[dict[str, Any]], entry: dict[str, Any]) -> bool:
        if entries and entries[-1]["preview"] == entry["preview"] and entry["line"] - entries[-1]["line"] <= 2:
            return False
        entries.append(entry)
        return True

    def compact_argument_preview(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            compact = " ".join(value.split())
        else:
            compact = json.dumps(value, sort_keys=True, ensure_ascii=True)
        preview, _ = truncate_text(compact, 240)
        return preview

    def flatten_message_text(self, value: Any) -> str | None:
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                text = self.flatten_message_text(item)
                if text:
                    parts.append(text)
            return "\n".join(parts).strip() or None
        if isinstance(value, dict):
            direct_text = value.get("text")
            if isinstance(direct_text, str) and direct_text.strip():
                return direct_text.strip()
            for key in ("content", "message"):
                nested = value.get(key)
                text = self.flatten_message_text(nested)
                if text:
                    return text
        return None

    def compact_preview(self, text: str | None) -> str | None:
        if not text:
            return None
        preview = " ".join(text.split())
        preview, _ = truncate_text(preview, 160)
        return preview

    def count_file_lines(self, path: Path) -> int:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)


def main() -> int:
    parser = argparse.ArgumentParser(description="Self-contained Ripcord recovery helper.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover = subparsers.add_parser("discover")
    discover.add_argument("--source", required=True, choices=["codex", "claude"])
    discover.add_argument("--workspace", required=True)
    discover.add_argument("--host", choices=["codex", "claude"])
    discover.add_argument("--limit", type=int, default=5)
    recover = subparsers.add_parser("recover")
    recover.add_argument("--source", required=True, choices=["codex", "claude"])
    recover.add_argument("--workspace", required=True)
    recover.add_argument("--session-id")
    recover.add_argument("--host", choices=["codex", "claude"])
    recover.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    engine = RecoveryEngine()
    if args.command == "discover":
        payload = engine.discover(
            workspace_root=args.workspace,
            source_provider=args.source,
            host_provider=args.host,
            limit=args.limit,
        )
    else:
        payload = engine.recover(
            workspace_root=args.workspace,
            source_provider=args.source,
            session_id=args.session_id,
            host_provider=args.host,
            limit=args.limit,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
