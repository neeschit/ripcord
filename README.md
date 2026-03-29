# Ripcord

**Hit a rate limit? Switch tools without losing your place.**

You're deep in a coding session with Claude Code. Rate limit. You switch to Codex — but now you're starting from zero. All that context, the plan, the recent changes, the conversation — gone.

Ripcord pulls the parachute. It recovers bounded context from your previous session so you can pick up where you left off in a different tool.


## How It Works

1. Detects sessions from the other tool in your current workspace.
2. Extracts a compact recovery bundle — initial prompt, recent messages, tool use, and metadata.
3. Surfaces the recovered context and lets you decide what to do next.

<img width="1707" height="912" alt="claude surface" src="https://github.com/user-attachments/assets/4a329364-3c0b-4e55-8cd4-b1e3dc80b136" />

No full transcript dumps. No automatic continuation. Just the context you need to keep moving.

## Quick Start

After installing either integration, restart that app before using it. If you install both sides, restart both Claude Code and Codex once.

### Claude Code → Recovering Codex Context

```text
/plugin marketplace add neeschit/ripcord
/plugin install ripcord-resume-codex@ripcord
```

Restart Claude Code after installing the plugin so `/resume-codex-session` is available.

Then:

```text
/resume-codex-session [session-id]
```

### Codex → Recovering Claude Context

Paste this into Codex:

```text
Install the skill from `skills/resume-claude-session` in the `neeschit/ripcord` repository using the GitHub skill installation flow. If the direct download method fails, retry with the git method.
```

Restart Codex after installing the skill so `resume-claude-session` is loaded.

Then:

```text
Use the `resume-claude-session` skill to recover context from a Claude session for this workspace.
```

## What Ripcord Won't Do

Ripcord is a recovery tool, not a session bridge. It won't:

- Restore provider-owned session state
- Resume shell or tool state
- Continue work automatically
- Pretend the two tools are interchangeable

The recovered bundle is designed to be **useful**, not to fake continuity.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for development workflow, testing, and local validation.

Inspired by [ralPhD](https://github.com/angadhn/ralPhd)

## License

[MIT License](LICENSE)
