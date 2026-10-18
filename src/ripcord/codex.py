import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .models import ArtifactRef, SessionDescriptor, TranscriptEvent
from .utils import canonicalize_path, epoch_to_iso, find_jsonl, is_wrapped_block, iter_jsonl, open_text, truncate_text


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
