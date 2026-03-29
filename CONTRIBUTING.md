# Contributing

Ripcord uses `uv` for environment management, dependency installation, and local commands.

## Prerequisites

- `uv`
- Python 3.13

## Setup

Create or update the local environment with:

```bash
uv sync
```

This installs the project plus the dev dependencies used by the test workflow.

## Running Tests

Run the test suite with:

```bash
uv run pytest
```

The suite is intentionally small and focused on high-signal recovery behavior and packaged helper smoke coverage.

## Local CLI Validation

Inspect the shared CLI:

```bash
uv run python -m ripcord.cli --help
```

Try discovery and recovery directly:

```bash
uv run python -m ripcord.cli discover --source claude --workspace /absolute/path
uv run python -m ripcord.cli recover --source codex --workspace /absolute/path --session-id <id> 
```

## Packaged Helper Validation

The Claude plugin and Codex skill each ship a bundled helper copy. Validate them directly with `uv run python`:

```bash
uv run python plugins/ripcord-resume-codex/helpers/ripcord_helper.py --help
uv run python skills/resume-claude-session/runtime/ripcord_helper.py --help
```

## Repo Layout

- `src/ripcord`: shared recovery logic and the `ripcord.cli` entrypoint
- `plugins/ripcord-resume-codex`: Claude plugin packaging and bundled helper
- `skills/resume-claude-session`: Codex skill packaging and bundled helper
- `tests`: contract-focused tests for recovery behavior and packaged helpers

## Maintenance Notes

- Keep the packaged helpers aligned with the shared implementation when recovery behavior changes.
- Keep `README.md`, `CONTRIBUTING.md`, plugin metadata, and skill docs consistent when install or usage flows change.
- Use `uv run python ...` for direct module and script execution in repo docs and contributor workflows.
