import json

import pytest

from ripcord.recovery import (
    MAX_RECENT_ASSISTANT_MESSAGES,
    MAX_RECENT_TOOL_USES,
    MAX_RECENT_USER_MESSAGES,
)
from support import RecoveryTestEnv, create_claude_session, create_codex_session


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


def test_warnings_only_cover_the_searched_provider(recovery_env: RecoveryTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(recovery_env.root / "missing-claude"))
    create_codex_session(recovery_env, session_id="codex-1", updated_epoch=100)

    result = recovery_env.recovery.recover(str(recovery_env.workspace), source_provider="codex", session_id="codex-1")

    assert result["warnings"] == []


def test_invalid_home_is_reported_once(recovery_env: RecoveryTestEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    missing_home = recovery_env.root / "missing-codex"
    monkeypatch.setenv("CODEX_HOME", str(missing_home))

    result = recovery_env.recovery.recover(str(recovery_env.workspace), source_provider="codex")

    assert result["warnings"] == [f"{missing_home} does not exist", "no matching sessions found"]
