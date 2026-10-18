---
name: resume-codex-session
description: Recover bounded context from a Codex session (including Codex in the ChatGPT app) for the current workspace and stop after asking whether to continue. Use only when explicitly invoked to resume work from Codex artifacts.
disable-model-invocation: true
argument-hint: "[session-id]"
allowed-tools: Bash, Read
---

Recover context from a Codex session, including sessions started from Codex in the ChatGPT app (they are stored in `~/.codex` too). This is a recovery workflow, not native session restoration.

Use this playbook:

1. Resolve the helper path.
   Prefer `${CLAUDE_SKILL_DIR}/../../helpers/ripcord_helper.py` when running from the installed plugin or a local checkout used with `claude --plugin-dir`.
   Otherwise find the installed helper with:
   `find ~/.claude/plugins/cache -path '*ripcord-resume-codex*/helpers/ripcord_helper.py' | head -n 1`
   If neither path exists, tell the user the plugin helper is unavailable and stop.

2. Build the recovery command for the current workspace.
   Run the helper with `uv run --script "<helper>"` when `uv` is on PATH (it fetches Python 3.14+ if needed); otherwise use `python3 "<helper>"`.
   Base arguments:
   `recover --source codex --workspace "$PWD"`
   If the user supplied a session id via `$ARGUMENTS[0]`, append:
   `--session-id "$ARGUMENTS[0]"`

3. Run the recovery command and inspect the JSON result.
   If `status` is `error`, show the `error` message to the user and stop.

4. If `status` is `not_found`, briefly explain that no Codex session matched this workspace and stop.

5. If `status` is `needs_selection`, present the ranked candidates with:
   `session_id`, `updated_at`, `preview`, `reasons`, and `workspace_match` (`exact`, `same_repo`, `nested`, or `unrelated`).
   Ask the user which session id to recover next. Do not guess.

6. If `status` is `ready`, compact and present only the bounded recovery fields:
   source session metadata (including `workspace_match` and `git_state`), `compaction_summary` when present, initial user prompt, recent user messages, recent assistant messages, recent reasoning, recent tool uses, and any warnings or truncation notes.
   Work from each message's `text`, not just its `preview`, so the reasoning behind earlier decisions carries over. Note any entry marked `truncated`.

7. End by asking whether to continue from that recovered point.
   Do not take action yet.
   Do not claim the Codex session has been restored natively.
   Do not dump the full raw transcript when the helper already returned bounded context.

Note:
When installed as a Claude plugin, this skill may appear under the plugin namespace in addition to the raw skill name.
