import re
from pathlib import Path
from typing import Any

from .models import ArtifactRef, SessionDescriptor, TranscriptEvent
from .utils import canonicalize_path, is_wrapped_block, iter_jsonl, mtime_to_iso, read_tail_records, truncate_text


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
