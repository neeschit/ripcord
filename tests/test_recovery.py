from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from ripcord.recovery import (
    MAX_RECENT_ASSISTANT_MESSAGES,
    MAX_RECENT_TOOL_USES,
    MAX_RECENT_USER_MESSAGES,
    RipcordRecovery,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class RecoveryTestEnv:
    root: Path
    workspace: Path
    codex_home: Path
    claude_home: Path
    recovery: RipcordRecovery


@pytest.fixture
def recovery_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RecoveryTestEnv:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "sessions").mkdir()
    (codex_home / "session_index.jsonl").write_text("", encoding="utf-8")

    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    (claude_home / "projects").mkdir()

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("RIPCORD_HOME", str(tmp_path / ".ripcord"))

    return RecoveryTestEnv(
        root=tmp_path,
        workspace=workspace,
        codex_home=codex_home,
        claude_home=claude_home,
        recovery=RipcordRecovery(),
    )


def test_codex_recover_returns_bounded_contract(recovery_env: RecoveryTestEnv) -> None:
    session_id = "codex-1"
    transcript_records = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": str(recovery_env.workspace)}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "first codex request"}],
            },
        },
    ]

    total_cycles = MAX_RECENT_TOOL_USES + 3
    for index in range(total_cycles):
        transcript_records.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"codex user {index}"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"codex assistant {index}"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": f"tool_{index}",
                        "arguments": json.dumps({"cmd": f"echo {index}"}),
                        "call_id": f"call-{index}",
                    },
                },
            ]
        )

    create_codex_session(
        recovery_env,
        session_id=session_id,
        updated_epoch=100,
        transcript_records=transcript_records,
    )

    result = recovery_env.recovery.recover(
        str(recovery_env.workspace),
        source_provider="codex",
        session_id=session_id,
    )

    assert result["status"] == "ready"
    assert result["initial_user_prompt"]["text"] == "first codex request"
    assert result["initial_user_prompt"]["truncated"] is False
    assert [entry["preview"] for entry in result["recent_user_messages"]] == [
        f"codex user {index}" for index in range(total_cycles - MAX_RECENT_USER_MESSAGES, total_cycles)
    ]
    assert [entry["preview"] for entry in result["recent_assistant_messages"]] == [
        f"codex assistant {index}" for index in range(total_cycles - MAX_RECENT_ASSISTANT_MESSAGES, total_cycles)
    ]
    assert [entry["name"] for entry in result["recent_tool_uses"]] == [
        f"tool_{index}" for index in range(total_cycles - MAX_RECENT_TOOL_USES, total_cycles)
    ]
    assert json.loads(result["recent_tool_uses"][-1]["arguments_preview"]) == {"cmd": "echo 10"}
    assert result["truncation"] == {
        "initial_user_prompt": False,
        "recent_user_messages": True,
        "recent_assistant_messages": True,
        "recent_tool_uses": True,
    }


def test_claude_recover_returns_bounded_contract(recovery_env: RecoveryTestEnv) -> None:
    session_id = "claude-1"
    transcript_records = [
        {
            "type": "system",
            "timestamp": "2026-03-29T10:00:00Z",
            "cwd": str(recovery_env.workspace),
            "sessionId": session_id,
            "gitBranch": "main",
        },
        {
            "type": "user",
            "timestamp": "2026-03-29T10:00:01Z",
            "cwd": str(recovery_env.workspace),
            "sessionId": session_id,
            "message": {"content": "first claude request"},
            "gitBranch": "main",
        },
    ]

    total_cycles = MAX_RECENT_TOOL_USES + 3
    for index in range(total_cycles):
        transcript_records.extend(
            [
                {
                    "type": "user",
                    "timestamp": f"2026-03-29T10:00:{index + 2:02d}Z",
                    "cwd": str(recovery_env.workspace),
                    "sessionId": session_id,
                    "message": {"content": f"claude user {index}"},
                    "gitBranch": "main",
                },
                {
                    "type": "assistant",
                    "timestamp": f"2026-03-29T10:10:{index:02d}Z",
                    "cwd": str(recovery_env.workspace),
                    "sessionId": session_id,
                    "message": [
                        {"type": "text", "text": f"claude assistant {index}"},
                        {"type": "tool_use", "name": f"claude_tool_{index}", "input": {"query": index}, "id": f"tool-{index}"},
                    ],
                    "gitBranch": "main",
                },
            ]
        )

    create_claude_session(
        recovery_env,
        session_id=session_id,
        updated_at="2026-03-29T11:00:00Z",
        transcript_records=transcript_records,
    )

    result = recovery_env.recovery.recover(
        str(recovery_env.workspace),
        source_provider="claude",
        session_id=session_id,
    )

    assert result["status"] == "ready"
    assert result["initial_user_prompt"]["text"] == "first claude request"
    assert [entry["preview"] for entry in result["recent_user_messages"]] == [
        f"claude user {index}" for index in range(total_cycles - MAX_RECENT_USER_MESSAGES, total_cycles)
    ]
    assert [entry["preview"] for entry in result["recent_assistant_messages"]] == [
        f"claude assistant {index}" for index in range(total_cycles - MAX_RECENT_ASSISTANT_MESSAGES, total_cycles)
    ]
    assert [entry["name"] for entry in result["recent_tool_uses"]] == [
        f"claude_tool_{index}" for index in range(total_cycles - MAX_RECENT_TOOL_USES, total_cycles)
    ]
    assert json.loads(result["recent_tool_uses"][-1]["arguments_preview"]) == {"query": 10}
    assert result["truncation"] == {
        "initial_user_prompt": False,
        "recent_user_messages": True,
        "recent_assistant_messages": True,
        "recent_tool_uses": True,
    }


def test_discover_returns_ranked_candidates_for_multiple_exact_matches(recovery_env: RecoveryTestEnv) -> None:
    create_claude_session(
        recovery_env,
        session_id="claude-older",
        updated_at="2026-03-29T10:00:00Z",
    )
    create_claude_session(
        recovery_env,
        session_id="claude-newer",
        updated_at="2026-03-29T11:00:00Z",
    )

    result = recovery_env.recovery.discover(str(recovery_env.workspace), source_provider="claude")

    assert result["status"] == "needs_selection"
    assert [candidate["session_id"] for candidate in result["candidates"]] == ["claude-newer", "claude-older"]


def test_recover_returns_not_found_for_missing_session(recovery_env: RecoveryTestEnv) -> None:
    result = recovery_env.recovery.recover(
        str(recovery_env.workspace),
        source_provider="codex",
        session_id="missing-session",
    )

    assert result["status"] == "not_found"
    assert result["warnings"] == ["session missing-session was not found"]


@pytest.mark.parametrize(
    ("helper_path", "source_provider", "session_id"),
    [
        ("plugins/ripcord-resume-codex/helpers/ripcord_helper.py", "codex", "codex-helper"),
        ("skills/resume-claude-session/runtime/ripcord_helper.py", "claude", "claude-helper"),
    ],
)
def test_packaged_helpers_can_recover_context(
    recovery_env: RecoveryTestEnv,
    helper_path: str,
    source_provider: str,
    session_id: str,
) -> None:
    if source_provider == "codex":
        create_codex_session(recovery_env, session_id=session_id, updated_epoch=200)
    else:
        create_claude_session(recovery_env, session_id=session_id, updated_at="2026-03-29T12:00:00Z")

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / helper_path),
            "recover",
            "--source",
            source_provider,
            "--workspace",
            str(recovery_env.workspace),
            "--session-id",
            session_id,
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=os.environ.copy(),
    )

    payload = json.loads(completed.stdout)
    assert payload["status"] == "ready"
    assert payload["session_id"] == session_id


def create_codex_session(
    recovery_env: RecoveryTestEnv,
    session_id: str,
    updated_epoch: int,
    transcript_records: list[dict[str, object]] | None = None,
) -> Path:
    transcript_dir = recovery_env.codex_home / "sessions" / "2026" / "03" / "29"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcript_dir / f"rollout-2026-03-29T10-00-00-{session_id}.jsonl"
    records = transcript_records or [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": str(recovery_env.workspace)}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "working"}],
            },
        },
    ]
    transcript_path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    (recovery_env.codex_home / "session_index.jsonl").write_text(
        json.dumps({"id": session_id, "thread_name": "test"}) + "\n",
        encoding="utf-8",
    )
    write_codex_sqlite_row(recovery_env, session_id, transcript_path, updated_epoch)
    return transcript_path


def write_codex_sqlite_row(
    recovery_env: RecoveryTestEnv,
    session_id: str,
    transcript_path: Path,
    updated_epoch: int,
) -> None:
    db_path = recovery_env.codex_home / "state_1.sqlite"
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
        connection.execute(
            """
            insert or replace into threads (
                id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                sandbox_policy, approval_mode, git_sha, git_branch, cli_version, first_user_message
            ) values (?, ?, ?, ?, 'vscode', 'openai', ?, 'Thread', 'workspace-write', 'default', 'abc123', 'main', '0.1', 'hello')
            """,
            (session_id, str(transcript_path), updated_epoch, updated_epoch, str(recovery_env.workspace)),
        )


def create_claude_session(
    recovery_env: RecoveryTestEnv,
    session_id: str,
    updated_at: str,
    transcript_records: list[dict[str, object]] | None = None,
) -> Path:
    project_dir = recovery_env.claude_home / "projects" / recovery_env.workspace.as_posix().replace("/", "-")
    project_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = project_dir / f"{session_id}.jsonl"
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
    transcript_path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    session_dir = project_dir / session_id / "subagents"
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
