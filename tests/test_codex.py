from typing import Any

from support import (
    RecoveryTestEnv,
    codex_assistant,
    codex_item,
    codex_meta,
    codex_reasoning,
    codex_token_count,
    codex_user,
    compress_with_zstd,
    create_codex_session,
    previews,
    write_codex_sqlite_schema,
)


def recover_codex(recovery_env: RecoveryTestEnv, records: list[dict[str, object]], session_id: str = "codex-1") -> dict[str, Any]:
    create_codex_session(
        recovery_env,
        session_id=session_id,
        updated_epoch=100,
        transcript_records=[codex_meta(session_id, recovery_env.workspace), *records],
    )
    return recovery_env.recovery.recover(str(recovery_env.workspace), source_provider="codex", session_id=session_id)


def test_injected_context_is_not_user_input(recovery_env: RecoveryTestEnv) -> None:
    result = recover_codex(
        recovery_env,
        [
            codex_user("<environment_context>\n  <cwd>/repo</cwd>\n  <shell>zsh</shell>\n</environment_context>"),
            codex_user("# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>\nUse uv.\n</INSTRUCTIONS>"),
            codex_user("<recommended_plugins>\n- github\n</recommended_plugins>"),
            codex_user("Add retries to the uploader"),
        ],
    )

    assert result["initial_user_prompt"]["text"] == "Add retries to the uploader"
    assert previews(result["recent_user_messages"]) == ["Add retries to the uploader"]


def test_item_completed_events_are_recovered(recovery_env: RecoveryTestEnv) -> None:
    result = recover_codex(
        recovery_env,
        [
            codex_item({"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": "Add retries", "text_elements": []}]}),
            codex_item({"type": "Reasoning", "id": "r1", "summary_text": ["Need exponential backoff"], "raw_content": []}),
            codex_item({"type": "AgentMessage", "id": "a1", "phase": "final_answer", "content": [{"type": "Text", "text": "Added backoff"}]}),
        ],
    )

    assert previews(result["recent_user_messages"]) == ["Add retries"]
    assert previews(result["recent_assistant_messages"]) == ["Added backoff"]
    assert [entry["text"] for entry in result.get("recent_reasoning", [])] == ["Need exponential backoff"]


def test_mirrored_records_are_not_duplicated(recovery_env: RecoveryTestEnv) -> None:
    result = recover_codex(
        recovery_env,
        [
            codex_user("Add retries"),
            codex_token_count(),
            codex_token_count(),
            codex_token_count(),
            codex_item({"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": "Add retries", "text_elements": []}]}),
            codex_reasoning("Need backoff"),
            codex_reasoning(""),
            codex_token_count(),
            codex_item({"type": "Reasoning", "id": "r1", "summary_text": ["Need backoff"], "raw_content": []}),
            codex_assistant("Added backoff"),
            codex_token_count(),
            codex_token_count(),
            codex_token_count(),
            codex_item({"type": "AgentMessage", "id": "a1", "phase": "final_answer", "content": [{"type": "Text", "text": "Added backoff"}]}),
        ],
    )

    assert previews(result["recent_user_messages"]) == ["Add retries"]
    assert previews(result["recent_assistant_messages"]) == ["Added backoff"]
    assert [entry["text"] for entry in result.get("recent_reasoning", [])] == ["Need backoff"]


def test_custom_tool_calls_are_recovered(recovery_env: RecoveryTestEnv) -> None:
    result = recover_codex(
        recovery_env,
        [
            codex_user("Patch it"),
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "input": "*** Begin Patch\n*** Update File: a.py\n*** End Patch",
                    "call_id": "call-1",
                    "status": "completed",
                },
            },
        ],
    )

    assert [(tool["name"], tool["call_id"]) for tool in result["recent_tool_uses"]] == [("apply_patch", "call-1")]
    assert "Begin Patch" in result["recent_tool_uses"][0]["arguments_preview"]


def test_compacted_record_becomes_compaction_summary(recovery_env: RecoveryTestEnv) -> None:
    result = recover_codex(
        recovery_env,
        [
            codex_user("Add retries"),
            {"type": "compacted", "payload": {"message": "Earlier: chose backoff over a circuit breaker."}},
        ],
    )

    assert (result.get("compaction_summary") or {}).get("text") == "Earlier: chose backoff over a circuit breaker."


def test_home_without_auth_json_is_usable(recovery_env: RecoveryTestEnv) -> None:
    (recovery_env.codex_home / "auth.json").unlink()
    create_codex_session(recovery_env, session_id="codex-1", updated_epoch=100)

    result = recovery_env.recovery.recover(str(recovery_env.workspace), source_provider="codex", session_id="codex-1")

    assert result["status"] == "ready"


def test_newest_state_database_is_used(recovery_env: RecoveryTestEnv) -> None:
    write_codex_sqlite_schema(recovery_env, "state_5.sqlite")
    create_codex_session(recovery_env, session_id="codex-1", updated_epoch=100, db_name="state_10.sqlite")

    result = recovery_env.recovery.recover(str(recovery_env.workspace), source_provider="codex", session_id="codex-1")

    assert result["status"] == "ready"


def test_rollouts_are_used_when_no_database_exists(recovery_env: RecoveryTestEnv) -> None:
    create_codex_session(recovery_env, session_id="codex-1", updated_epoch=100, write_db=False)

    result = recovery_env.recovery.discover(str(recovery_env.workspace), source_provider="codex")

    assert result["status"] == "ready"
    assert result["session"]["session_id"] == "codex-1"


def test_subagent_threads_are_not_candidates(recovery_env: RecoveryTestEnv) -> None:
    create_codex_session(recovery_env, session_id="main", updated_epoch=100)
    create_codex_session(recovery_env, session_id="guardian", updated_epoch=200, source='{"subagent":{"other":"guardian"}}')

    result = recovery_env.recovery.discover(str(recovery_env.workspace), source_provider="codex")

    assert result["status"] == "ready"
    assert result["session"]["session_id"] == "main"


def test_compressed_rollout_is_recovered(recovery_env: RecoveryTestEnv) -> None:
    compress_with_zstd(create_codex_session(recovery_env, session_id="codex-1", updated_epoch=100))

    result = recovery_env.recovery.recover(str(recovery_env.workspace), source_provider="codex", session_id="codex-1")

    assert result["warnings"] == []
    assert result["transcript_metadata"]["exists"] is True
    assert previews(result["recent_user_messages"]) == ["hello"]


def test_compressed_rollouts_are_found_without_database(recovery_env: RecoveryTestEnv) -> None:
    compress_with_zstd(create_codex_session(recovery_env, session_id="codex-1", updated_epoch=100, write_db=False))

    result = recovery_env.recovery.discover(str(recovery_env.workspace), source_provider="codex")

    assert result["status"] == "ready"
    assert result["session"]["session_id"] == "codex-1"


def test_injected_skill_body_is_reduced_to_its_name(recovery_env: RecoveryTestEnv) -> None:
    result = recover_codex(
        recovery_env,
        [
            codex_user(
                "<skill>\n<name>resume-claude-session</name>\n<path>/skills/resume-claude-session/SKILL.md</path>\n"
                "Recover context from a Claude session...\n</skill>"
            ),
            codex_user("Use the skill to recover context"),
        ],
    )

    assert previews(result["recent_user_messages"]) == ["[skill] resume-claude-session", "Use the skill to recover context"]
    assert result["initial_user_prompt"]["text"] == "Use the skill to recover context"
