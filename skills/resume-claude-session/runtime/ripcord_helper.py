#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# ///
"""Ripcord recovery helper.

Generated from src/ripcord by scripts/build_helpers.py. Do not edit by hand.
"""

import sys

if sys.version_info < (3, 14):
    import json

    print(json.dumps({"status": "error", "error": f"ripcord requires Python 3.14+; found {sys.version.split()[0]}"}))
    raise SystemExit(2)

import argparse
import json
import os
import re
import sqlite3
from collections import deque
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, TextIO


# --- ripcord/utils.py ---

try:
    from compression import zstd
except ImportError:  # Python built without libzstd
    zstd = None


# Harness-injected context arrives as a single block wrapped in a tag like
# <system-reminder> or <environment_context>; the hyphen/underscore keeps pasted HTML out.
WRAPPED_BLOCK = re.compile(r"\A<([A-Za-z][A-Za-z0-9]*[-_][\w-]*)>.*</\1>\Z", re.DOTALL)


def expand_path(value: str | Path) -> Path:
    return Path(value).expanduser()


def canonicalize_path(value: str | Path | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        return str(expand_path(value).resolve())
    except FileNotFoundError:
        return str(expand_path(value).absolute())


def open_text(path: Path) -> TextIO:
    if path.suffix == ".zst":
        if zstd is None:
            raise OSError(f"cannot read {path}: this Python has no zstd support")
        return zstd.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def find_jsonl(path: Path) -> Path:
    """Return path, or its zstd-compressed sibling when only that exists (Codex compresses old rollouts)."""
    compressed = path.with_name(path.name + ".zst")
    if not path.exists() and zstd is not None and compressed.exists():
        return compressed
    return path


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with open_text(path) as handle:
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


def read_tail_records(path: Path, max_bytes: int = 65536) -> list[dict[str, Any]]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(size - max_bytes, 0))
        chunk = handle.read()
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes:
        lines = lines[1:]
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def epoch_to_iso(value: int | float | None) -> str:
    if value is None:
        return ""
    if value > 10_000_000_000:
        value = value / 1000
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def mtime_to_iso(path: Path) -> str:
    return epoch_to_iso(path.stat().st_mtime)


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit <= 3:
        return text[:limit], True
    return text[: limit - 3] + "...", True


def compact_preview(text: str) -> str:
    preview, _ = truncate_text(" ".join(text.split()), 160)
    return preview


def is_wrapped_block(text: str) -> bool:
    return WRAPPED_BLOCK.match(text.strip()) is not None


# --- ripcord/models.py ---

@dataclass(slots=True)
class HomeStatus:
    path: str
    exists: bool
    valid: bool
    markers_present: list[str] = field(default_factory=list)
    missing_markers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
class TranscriptEvent:
    kind: str
    text: str | None = None
    name: str | None = None
    arguments: Any = None
    call_id: str | None = None
    is_command: bool = False


@dataclass(slots=True)
class TranscriptHints:
    total_lines: int
    first_user_line: int | None = None
    first_user_preview: str | None = None
    last_user_line: int | None = None
    last_user_preview: str | None = None
    last_assistant_line: int | None = None
    last_assistant_preview: str | None = None

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
    workspace_match: str
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifact_refs: list[ArtifactRef] = field(default_factory=list)

    def to_candidate(self, score: int, workspace_match: str, workspace_reason: str) -> CandidateSession:
        return CandidateSession(
            provider=self.provider,
            session_id=self.session_id,
            workspace_root=self.workspace_root,
            updated_at=self.updated_at,
            git_branch=self.git_branch,
            git_sha=self.git_sha,
            score=score,
            preview=self.preview,
            reasons=[workspace_reason, *self.reasons],
            exact_workspace_match=workspace_match == "exact",
            workspace_match=workspace_match,
            artifact_refs=list(self.artifact_refs),
            warnings=list(self.warnings),
        )


@dataclass(slots=True)
class HomesReport:
    homes: dict[str, HomeStatus]
    workspace_root: str
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            **{f"{name}_home": status.to_dict() for name, status in self.homes.items()},
            "workspace_root": self.workspace_root,
            "warnings": list(self.warnings),
        }


# --- ripcord/workspace.py ---

WORKSPACE_MATCH_SCORES = {
    "exact": 1000,
    "same_repo": 500,
    "nested": 400,
    "unrelated": 0,
}
WORKSPACE_MATCH_REASONS = {
    "exact": "exact workspace match",
    "same_repo": "same git repository (another worktree or subdirectory)",
    "nested": "parent or child directory of the workspace",
    "unrelated": "different workspace",
}


class WorkspaceMatcher:
    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = canonicalize_path(workspace_root) or workspace_root
        self._git_dirs: dict[str, str | None] = {}
        self._workspace_git_dir = self._git_common_dir(self.workspace_root)

    def relation(self, cwd: str) -> str:
        candidate = canonicalize_path(cwd)
        if candidate is None:
            return "unrelated"
        if candidate == self.workspace_root:
            return "exact"
        if self._workspace_git_dir is not None and self._git_common_dir(candidate) == self._workspace_git_dir:
            return "same_repo"
        workspace, other = Path(self.workspace_root), Path(candidate)
        if workspace in other.parents or other in workspace.parents:
            return "nested"
        return "unrelated"

    def _git_common_dir(self, path: str) -> str | None:
        if path not in self._git_dirs:
            self._git_dirs[path] = _find_git_common_dir(Path(path))
        return self._git_dirs[path]


def _find_git_common_dir(path: Path) -> str | None:
    for directory in (path, *path.parents):
        dot_git = directory / ".git"
        try:
            if dot_git.is_dir():
                return str(dot_git.resolve())
            if dot_git.is_file():
                return _resolve_gitdir_file(directory, dot_git)
        except OSError:
            return None
    return None


def _resolve_gitdir_file(directory: Path, dot_git: Path) -> str | None:
    content = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    if not content.startswith("gitdir:"):
        return None
    gitdir = Path(content.removeprefix("gitdir:").strip())
    if not gitdir.is_absolute():
        gitdir = directory / gitdir
    commondir_file = gitdir / "commondir"
    if commondir_file.is_file():
        common = Path(commondir_file.read_text(encoding="utf-8").strip())
        gitdir = common if common.is_absolute() else gitdir / common
    return str(gitdir.resolve())


# --- ripcord/codex.py ---

CODEX_INJECTED_PREFIXES = ("# AGENTS.md instructions",)
CODEX_SKILL_NAME = re.compile(r"\A<skill>\s*<name>(.*?)</name>.*</skill>\Z", re.DOTALL)
CODEX_STATE_DB = re.compile(r"state_(\d+)\.sqlite")
CODEX_THREAD_COLUMNS = (
    "id",
    "rollout_path",
    "updated_at",
    "cwd",
    "title",
    "git_branch",
    "git_sha",
    "first_user_message",
    "source",
    "thread_source",
)
CODEX_SUBAGENT_THREAD_SOURCES = {"subagent", "guardian_review"}


class CodexProvider:
    name = "codex"
    home_env = "CODEX_HOME"
    default_home = "~/.codex"
    required_markers = ["sessions"]
    optional_markers = ["shell_snapshots", "session_index.jsonl", "state_*.sqlite"]

    def load_sessions(self, home: Path, session_id: str | None = None) -> list[SessionDescriptor]:
        results: list[SessionDescriptor] = []
        for row in _read_codex_threads(home, session_id=session_id):
            current_session_id = row["id"]
            if session_id and current_session_id != session_id:
                continue
            if _is_codex_subagent_thread(row):
                continue

            cwd = canonicalize_path(row.get("cwd")) or ""
            rollout_path = row.get("rollout_path") or ""
            transcript_path = find_jsonl(Path(rollout_path)) if rollout_path else None
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

            shell_glob = home / "shell_snapshots"
            shell_refs = sorted(shell_glob.glob(f"{current_session_id}.*.sh")) if shell_glob.exists() else []
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

            reasons: list[str] = []
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
                    reasons=reasons,
                    warnings=warnings,
                    artifact_refs=artifact_refs,
                )
            )
        return results

    def parse_record(self, record: dict[str, Any]) -> list[TranscriptEvent]:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return []
        record_type = record.get("type")
        if record_type == "response_item":
            return _codex_response_item_events(payload)
        if record_type == "event_msg":
            return _codex_event_msg_events(payload)
        if record_type == "compacted":
            text = _codex_text(payload.get("message"))
            return [TranscriptEvent(kind="summary", text=text)] if text else []
        return []


def _codex_response_item_events(payload: dict[str, Any]) -> list[TranscriptEvent]:
    item_type = payload.get("type")
    if item_type == "message":
        text = _codex_text(payload.get("content"))
        if payload.get("role") == "user":
            return _codex_user_events(text)
        if payload.get("role") == "assistant" and text:
            return [TranscriptEvent(kind="assistant", text=text)]
        return []
    if item_type == "reasoning":
        text = _codex_text(payload.get("summary"))
        return [TranscriptEvent(kind="reasoning", text=text)] if text else []
    if item_type in {"function_call", "custom_tool_call"}:
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            return []
        call_id = payload.get("call_id")
        return [
            TranscriptEvent(
                kind="tool_use",
                name=name,
                arguments=payload.get("arguments") if item_type == "function_call" else payload.get("input"),
                call_id=call_id if isinstance(call_id, str) else None,
            )
        ]
    return []


def _codex_event_msg_events(payload: dict[str, Any]) -> list[TranscriptEvent]:
    event_type = payload.get("type")
    if event_type == "user_message":
        return _codex_user_events(_codex_text(payload.get("message")))
    if event_type == "agent_message":
        text = _codex_text(payload.get("message"))
        return [TranscriptEvent(kind="assistant", text=text)] if text else []
    if event_type != "item_completed" or not isinstance(payload.get("item"), dict):
        return []
    item = payload["item"]
    if item.get("type") == "UserMessage":
        return _codex_user_events(_codex_text(item.get("content")))
    if item.get("type") == "AgentMessage":
        text = _codex_text(item.get("content"))
        return [TranscriptEvent(kind="assistant", text=text)] if text else []
    if item.get("type") == "Reasoning":
        text = _codex_text(item.get("summary_text"))
        return [TranscriptEvent(kind="reasoning", text=text)] if text else []
    return []


def _codex_user_events(text: str | None) -> list[TranscriptEvent]:
    if not text or text.startswith(CODEX_INJECTED_PREFIXES) or is_wrapped_block(text):
        return []
    skill = CODEX_SKILL_NAME.match(text)
    if skill is not None:
        return [TranscriptEvent(kind="user", text=f"[skill] {skill.group(1).strip()}", is_command=True)]
    return [TranscriptEvent(kind="user", text=text)]


def _codex_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        parts = [part if isinstance(part, str) else part.get("text") for part in value if isinstance(part, (str, dict))]
        text = "\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
        return text or None
    return None


def _is_codex_subagent_thread(row: dict[str, Any]) -> bool:
    if row.get("thread_source") in CODEX_SUBAGENT_THREAD_SOURCES:
        return True
    source = row.get("source")
    if isinstance(source, str) and source.startswith("{"):
        try:
            source = json.loads(source)
        except json.JSONDecodeError:
            return False
    return isinstance(source, dict) and "subagent" in source


def _read_codex_threads(home: Path, session_id: str | None = None) -> list[dict[str, Any]]:
    database_path = _latest_codex_state_database(home)
    if database_path is not None:
        rows = _query_codex_threads(database_path, session_id=session_id)
        if rows is not None:
            return rows
    return _read_codex_rollouts(home)


def _latest_codex_state_database(home: Path) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    for path in home.glob("state_*.sqlite"):
        match = CODEX_STATE_DB.fullmatch(path.name)
        if match:
            candidates.append((int(match.group(1)), path))
    return max(candidates)[1] if candidates else None


def _query_codex_threads(database_path: Path, session_id: str | None = None) -> list[dict[str, Any]] | None:
    try:
        with closing(sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            available = {row["name"] for row in conn.execute("pragma table_info(threads)")}
            if not {"id", "rollout_path", "cwd"} <= available:
                return None
            columns = [column for column in CODEX_THREAD_COLUMNS if column in available]
            conditions: list[str] = []
            params: list[str] = []
            if "archived" in available:
                conditions.append("archived = 0")
            if session_id:
                conditions.append("id = ?")
                params.append(session_id)
            query = f"select {', '.join(columns)} from threads"
            if conditions:
                query += " where " + " and ".join(conditions)
            if "updated_at" in available:
                query += " order by updated_at desc"
            rows = conn.execute(query, params).fetchall()
    except sqlite3.Error:
        return None
    return [dict(row) for row in rows]


def _read_codex_rollouts(home: Path) -> list[dict[str, Any]]:
    titles: dict[str, str] = {}
    index_path = home / "session_index.jsonl"
    if index_path.exists():
        for entry in iter_jsonl(index_path):
            if isinstance(entry.get("id"), str):
                titles[entry["id"]] = entry.get("thread_name") or ""

    rows: dict[str, dict[str, Any]] = {}
    for path in [*home.glob("sessions/**/*.jsonl"), *home.glob("sessions/**/*.jsonl.zst")]:
        meta = _read_codex_session_meta(path)
        session_id = meta.get("id") if meta else None
        if not isinstance(session_id, str) or not session_id or session_id in rows:
            continue
        git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
        rows[session_id] = {
            "id": session_id,
            "rollout_path": str(path),
            "updated_at": path.stat().st_mtime,
            "cwd": meta.get("cwd") or "",
            "title": titles.get(session_id) or session_id,
            "git_branch": git.get("branch"),
            "git_sha": git.get("commit_hash"),
            "first_user_message": titles.get(session_id) or "",
            "source": meta.get("source"),
        }
    return sorted(rows.values(), key=lambda row: row["updated_at"], reverse=True)


def _read_codex_session_meta(path: Path) -> dict[str, Any] | None:
    try:
        with open_text(path) as handle:
            first_line = handle.readline().strip()
    except (OSError, EOFError):
        return None
    try:
        record = json.loads(first_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


# --- ripcord/claude.py ---

CLAUDE_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
CLAUDE_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


class ClaudeProvider:
    name = "claude"
    home_env = "CLAUDE_HOME"
    default_home = "~/.claude"
    required_markers = ["projects"]
    optional_markers = ["usage-data/session-meta", "file-history", "shell-snapshots"]

    def load_sessions(self, home: Path, session_id: str | None = None) -> list[SessionDescriptor]:
        results: list[SessionDescriptor] = []
        for transcript_path in sorted(home.glob("projects/**/*.jsonl")):
            if "subagents" in transcript_path.parts:
                continue
            current_session_id = transcript_path.stem
            if session_id and current_session_id != session_id:
                continue

            cwd, preview, updated_at, git_branch = self._read_transcript_metadata(transcript_path)
            artifact_refs: list[ArtifactRef] = [
                ArtifactRef(
                    provider="claude",
                    session_id=current_session_id,
                    kind="transcript",
                    path=str(transcript_path),
                    label="Claude project transcript",
                    is_primary=True,
                )
            ]

            meta_path = home / "usage-data" / "session-meta" / f"{current_session_id}.json"
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
            subagent_refs = sorted((session_dir / "subagents").glob("*.jsonl")) if session_dir.exists() else []
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

            file_history_root = home / "file-history" / current_session_id
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
            reasons = ["primary transcript present"]
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
                    updated_at=updated_at,
                    git_branch=git_branch,
                    git_sha=None,
                    preview=preview,
                    completeness_score=25,
                    enrichment_score=(2 if has_meta else 0) + len(subagent_refs) * 2 + (2 if has_file_history else 0),
                    reasons=reasons,
                    artifact_refs=artifact_refs,
                )
            )
        return results

    def parse_record(self, record: dict[str, Any]) -> list[TranscriptEvent]:
        if record.get("isMeta"):
            return []
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else message
        if record.get("type") == "user":
            text = _claude_user_text(content)
            if text is None:
                return []
            if record.get("isCompactSummary"):
                return [TranscriptEvent(kind="summary", text=text)]
            command = _render_claude_command(text)
            if command is not None:
                return [TranscriptEvent(kind="user", text=command, is_command=True)]
            if is_wrapped_block(text):
                return []
            return [TranscriptEvent(kind="user", text=text)]
        if record.get("type") == "assistant":
            return _claude_assistant_events(content)
        return []

    def _read_transcript_metadata(self, transcript_path: Path) -> tuple[str, str, str, str | None]:
        cwd = ""
        git_branch = None
        preview = None
        command_preview = None
        for record in iter_jsonl(transcript_path):
            if not cwd and isinstance(record.get("cwd"), str):
                cwd = record["cwd"]
            git_branch = record.get("gitBranch", git_branch)
            for event in self.parse_record(record):
                if event.kind != "user":
                    continue
                if event.is_command:
                    command_preview = command_preview or event.text
                else:
                    preview = event.text
                    break
            if preview:
                break

        updated_at = ""
        for record in reversed(read_tail_records(transcript_path)):
            if isinstance(record.get("timestamp"), str):
                updated_at = record["timestamp"]
                git_branch = record.get("gitBranch", git_branch)
                break
        return cwd, preview or command_preview or transcript_path.stem, updated_at or mtime_to_iso(transcript_path), git_branch


def _claude_user_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip() and not is_wrapped_block(text):
            parts.append(text.strip())
    return "\n".join(parts) or None


def _render_claude_command(text: str) -> str | None:
    if not text.startswith("<command-"):
        return None
    name = CLAUDE_COMMAND_NAME.search(text)
    if name is None:
        return None
    args = CLAUDE_COMMAND_ARGS.search(text)
    return f"{name.group(1).strip()} {args.group(1).strip() if args else ''}".strip()


def _claude_assistant_events(content: Any) -> list[TranscriptEvent]:
    if isinstance(content, str):
        return [TranscriptEvent(kind="assistant", text=content.strip())] if content.strip() else []
    if not isinstance(content, list):
        return []
    events: list[TranscriptEvent] = []
    texts: list[str] = []
    tool_uses: list[TranscriptEvent] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str) and block["text"].strip():
            texts.append(block["text"].strip())
        elif block_type == "thinking" and isinstance(block.get("thinking"), str) and block["thinking"].strip():
            events.append(TranscriptEvent(kind="reasoning", text=block["thinking"].strip()))
        elif block_type == "tool_use" and isinstance(block.get("name"), str) and block["name"]:
            tool_uses.append(
                TranscriptEvent(
                    kind="tool_use",
                    name=block["name"],
                    arguments=block.get("input"),
                    call_id=block.get("id") if isinstance(block.get("id"), str) else None,
                )
            )
    if texts:
        events.append(TranscriptEvent(kind="assistant", text="\n".join(texts)))
    return events + tool_uses


# --- ripcord/providers.py ---

class Provider(Protocol):
    name: str
    home_env: str
    default_home: str
    required_markers: list[str]
    optional_markers: list[str]

    def load_sessions(self, home: Path, session_id: str | None = None) -> list[SessionDescriptor]: ...

    def parse_record(self, record: dict[str, Any]) -> list[TranscriptEvent]: ...


PROVIDERS: dict[str, Provider] = {provider.name: provider for provider in (CodexProvider(), ClaudeProvider())}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(f"unsupported provider: {name}") from None


# --- ripcord/homes.py ---

def _validate_home(path: Path, required_markers: list[str], optional_markers: list[str]) -> HomeStatus:
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

    return HomeStatus(
        path=canonicalize_path(path) or str(path),
        exists=exists,
        valid=exists and not missing_required,
        markers_present=present_required + present_optional,
        missing_markers=missing_required,
        warnings=warnings,
    )


def resolve_homes(workspace_root: str | None = None) -> HomesReport:
    workspace = expand_path(workspace_root or os.getcwd())
    homes = {
        name: _validate_home(
            expand_path(os.environ.get(provider.home_env, provider.default_home)),
            required_markers=provider.required_markers,
            optional_markers=provider.optional_markers,
        )
        for name, provider in PROVIDERS.items()
    }

    return HomesReport(
        homes=homes,
        workspace_root=canonicalize_path(workspace) or str(workspace),
        warnings=[warning for status in homes.values() for warning in status.warnings],
    )


# --- ripcord/recovery.py ---

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


# --- ripcord/cli.py ---

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recover bounded foreign-session context for Ripcord skills.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="Find foreign sessions for a workspace.")
    discover.add_argument("--source", required=True, choices=["codex", "claude"])
    discover.add_argument("--workspace", required=True)
    discover.add_argument("--host", choices=["codex", "claude"])
    discover.add_argument("--limit", type=int, default=5)
    recover = subparsers.add_parser("recover", help="Recover a bounded foreign-session bundle.")
    recover.add_argument("--source", required=True, choices=["codex", "claude"])
    recover.add_argument("--workspace", required=True)
    recover.add_argument("--session-id")
    recover.add_argument("--host", choices=["codex", "claude"])
    recover.add_argument("--limit", type=int, default=5)

    return parser


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    recovery = RipcordRecovery()

    if args.command == "discover":
        payload = recovery.discover(
            workspace_root=args.workspace,
            source_provider=args.source,
            host_provider=args.host,
            limit=args.limit,
        )
        return _emit(payload)

    payload = recovery.recover(
        workspace_root=args.workspace,
        source_provider=args.source,
        session_id=args.session_id,
        host_provider=args.host,
        limit=args.limit,
    )
    return _emit(payload)


if __name__ == "__main__":
    raise SystemExit(main())
