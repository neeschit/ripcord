---
name: resume-claude-session
description: Recover bounded context from a Claude session for the current workspace and stop after asking whether to continue. Use when resuming work from Claude artifacts inside Codex.
---

Recover context from a Claude session. This is a recovery workflow, not native session restoration.

Use this playbook:

1. Resolve the helper path.
   Prefer `./skills/resume-claude-session/runtime/ripcord_helper.py` when the current workspace is this repository.
   Otherwise use the installed skill helper at `~/.codex/skills/resume-claude-session/runtime/ripcord_helper.py`.
   If neither path exists, tell the user the skill helper is unavailable and stop.

2. Build the recovery command for the current workspace.
   Run the helper with `uv run --script "<helper>"` when `uv` is on PATH (it fetches Python 3.14+ if needed); otherwise use `python3 "<helper>"`.
   Base arguments:
   `recover --source claude --workspace "$PWD"`
   If the user already supplied a specific Claude session id, append:
   `--session-id "<session-id>"`

3. Run the recovery command and inspect the JSON result.
   If `status` is `error`, show the `error` message to the user and stop.

4. If `status` is `not_found`, briefly explain that no Claude session matched this workspace and stop.

5. If `status` is `needs_selection`, present the ranked candidates with:
   `session_id`, `updated_at`, `preview`, `reasons`, and `workspace_match` (`exact`, `same_repo`, `nested`, or `unrelated`).
   Ask the user which session id to recover next. Do not guess.

6. If `status` is `ready`, compact and present only the bounded recovery fields:
   source session metadata (including `workspace_match` and `git_state`), `compaction_summary` when present, initial user prompt, recent user messages, recent assistant messages, recent reasoning, recent tool uses, and any warnings or truncation notes.
   Work from each message's `text`, not just its `preview`, so the reasoning behind earlier decisions carries over. Note any entry marked `truncated`.

7. End by asking whether to continue from that recovered point.
   Do not take action yet.
   Do not claim the Claude session has been restored natively.
   Do not dump the full raw transcript when the helper already returned bounded context.
