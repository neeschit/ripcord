import json
import sqlite3
from compression import zstd
from dataclasses import dataclass
from pathlib import Path

from ripcord.recovery import RipcordRecovery


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class RecoveryTestEnv:
    root: Path
    workspace: Path
    codex_home: Path
    claude_home: Path
    recovery: RipcordRecovery


def write_jsonl(path: Path, records: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def compress_with_zstd(path: Path) -> Path:
    compressed = path.with_name(path.name + ".zst")
    compressed.write_bytes(zstd.compress(path.read_bytes()))
    path.unlink()
    return compressed


def previews(entries: list[dict[str, object]]) -> list[object]:
    return [entry["preview"] for entry in entries]


# Claude Code transcripts


def claude_record(
    recovery_env: RecoveryTestEnv,
    kind: str,
    content: object,
    timestamp: str = "2026-03-29T10:00:00Z",
    session_id: str = "claude-1",
    **extra: object,
) -> dict[str, object]:
    return {
        "type": kind,
        "timestamp": timestamp,
        "cwd": str(recovery_env.workspace),
        "sessionId": session_id,
        "gitBranch": "main",
        "message": {"role": kind, "content": content},
        **extra,
    }


def claude_project_dir(recovery_env: RecoveryTestEnv) -> Path:
    return recovery_env.claude_home / "projects" / recovery_env.workspace.as_posix().replace("/", "-")


def write_claude_transcript(recovery_env: RecoveryTestEnv, session_id: str, records: list[dict[str, object]]) -> Path:
    return write_jsonl(claude_project_dir(recovery_env) / f"{session_id}.jsonl", records)


def create_claude_session(
    recovery_env: RecoveryTestEnv,
    session_id: str,
    updated_at: str,
    transcript_records: list[dict[str, object]] | None = None,
) -> Path:
    records = transcript_records or [
        {
            "type": "system",
            "timestamp": updated_at,
            "cwd": str(recovery_env.workspace),
            "sessionId": session_id,
            "gitBranch": "main",
        },
        {
            "type": "user",
            "timestamp": updated_at,
            "cwd": str(recovery_env.workspace),
            "sessionId": session_id,
            "message": {"content": "resume this work"},
            "gitBranch": "main",
        },
    ]
    transcript_path = write_claude_transcript(recovery_env, session_id, records)

    session_dir = claude_project_dir(recovery_env) / session_id / "subagents"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "agent-1.jsonl").write_text("{}", encoding="utf-8")

    metadata_dir = recovery_env.claude_home / "usage-data" / "session-meta"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / f"{session_id}.json").write_text(
        json.dumps({"session_id": session_id, "project_path": str(recovery_env.workspace), "start_time": updated_at}),
        encoding="utf-8",
    )

    (recovery_env.claude_home / "file-history" / session_id).mkdir(parents=True, exist_ok=True)
    return transcript_path


# Codex rollouts


def codex_meta(session_id: str, cwd: Path, source: object = "vscode") -> dict[str, object]:
    return {"type": "session_meta", "payload": {"id": session_id, "cwd": str(cwd), "source": source}}


def codex_user(text: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
    }


def codex_assistant(text: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
    }


def codex_reasoning(text: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": text}] if text else [],
            "content": None,
            "encrypted_content": "opaque",
        },
    }


def codex_item(item: dict[str, object]) -> dict[str, object]:
    return {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": "t", "turn_id": "turn", "item": item}}


def codex_token_count() -> dict[str, object]:
    return {"type": "event_msg", "payload": {"type": "token_count", "info": None}}


def create_codex_session(
    recovery_env: RecoveryTestEnv,
    session_id: str,
    updated_epoch: int,
    transcript_records: list[dict[str, object]] | None = None,
    cwd: Path | None = None,
    source: str = "vscode",
    db_name: str = "state_1.sqlite",
    write_db: bool = True,
) -> Path:
    cwd = cwd or recovery_env.workspace
    transcript_dir = recovery_env.codex_home / "sessions" / "2026" / "03" / "29"
    transcript_path = transcript_dir / f"rollout-2026-03-29T10-00-00-{session_id}.jsonl"
    meta_source = json.loads(source) if source.startswith("{") else source
    records = transcript_records or [
        codex_meta(session_id, cwd, source=meta_source),
        codex_user("hello"),
        codex_assistant("working"),
    ]
    write_jsonl(transcript_path, records)
    with (recovery_env.codex_home / "session_index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": session_id, "thread_name": "test", "updated_at": "2026-03-29T10:00:00Z"}) + "\n")
    if write_db:
        write_codex_sqlite_row(recovery_env, session_id, transcript_path, updated_epoch, cwd=cwd, source=source, db_name=db_name)
    return transcript_path


def write_codex_sqlite_schema(recovery_env: RecoveryTestEnv, db_name: str = "state_1.sqlite") -> Path:
    db_path = recovery_env.codex_home / db_name
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            create table if not exists threads (
                id text primary key,
                rollout_path text not null,
                created_at integer not null,
                updated_at integer not null,
                source text not null,
                model_provider text not null,
                cwd text not null,
                title text not null,
                sandbox_policy text not null,
                approval_mode text not null,
                tokens_used integer not null default 0,
                has_user_event integer not null default 0,
                archived integer not null default 0,
                archived_at integer,
                git_sha text,
                git_branch text,
                git_origin_url text,
                cli_version text not null default '',
                first_user_message text not null default '',
                agent_nickname text,
                agent_role text,
                memory_mode text not null default 'enabled',
                model text,
                reasoning_effort text,
                agent_path text
            )
            """
        )
    return db_path


def write_codex_sqlite_row(
    recovery_env: RecoveryTestEnv,
    session_id: str,
    transcript_path: Path,
    updated_epoch: int,
    cwd: Path | None = None,
    source: str = "vscode",
    db_name: str = "state_1.sqlite",
) -> None:
    db_path = write_codex_sqlite_schema(recovery_env, db_name)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            insert or replace into threads (
                id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                sandbox_policy, approval_mode, git_sha, git_branch, cli_version, first_user_message
            ) values (?, ?, ?, ?, ?, 'openai', ?, 'Thread', 'workspace-write', 'default', 'abc123', 'main', '0.1', 'hello')
            """,
            (session_id, str(transcript_path), updated_epoch, updated_epoch, source, str(cwd or recovery_env.workspace)),
        )
