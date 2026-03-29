# Ripcord

Ripcord helps you recover bounded context when switching between Codex and Claude in the same workspace.

It is a recovery tool, not a native session handoff. Ripcord finds relevant local session artifacts, extracts a compact recovery bundle, presents the important context, and stops before taking new action.

## What Ripcord Does

- Finds likely foreign sessions for the current workspace.
- Extracts a bounded bundle instead of dumping full transcripts.
- Recovers the initial prompt, recent user and assistant messages, recent tool use, and session metadata.
- Stops after surfacing the recovered context so the user can decide whether to continue.

## What Ripcord Does Not Do

- Restore provider-owned session state.
- Resume tools or shell state natively.
- Continue work automatically after recovery.
- Treat Codex and Claude sessions as interchangeable.

## Supported Surfaces

### Claude Code: Recover Codex Context

Ripcord ships a Claude plugin named `ripcord-resume-codex`.

Install it from GitHub:

```text
/plugin marketplace add neeschit/ripcord
/plugin install ripcord-resume-codex@ripcord
```

Depending on Claude's plugin namespacing, the skill may appear as `resume-codex-session` or `/ripcord-resume-codex:resume-codex-session`.

### Codex: Recover Claude Context

Ripcord also ships a Codex skill named `resume-claude-session`.

Install the skill from the GitHub path `skills/resume-claude-session` in the `neeschit/ripcord` repository using Codex's GitHub skill installation flow.

## Recovery Model

Ripcord is intentionally conservative:

1. Discover likely sessions for the current workspace.
2. Select the best match or ask the user to choose.
3. Recover bounded context from the foreign transcript.
4. Present the result and ask whether to continue.

The recovered bundle is designed to be useful without pretending the original session has been restored.

## Development

Development workflow, testing, and local validation live in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Ripcord is licensed under the [MIT License](LICENSE).
