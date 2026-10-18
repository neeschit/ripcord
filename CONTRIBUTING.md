# Contributing

Ripcord uses `uv` for environment management, dependency installation, and local commands.

## Prerequisites

- `uv`
- Python 3.14+

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

## Packaged Helpers

The Codex skill ships a single-file helper generated from `src/ripcord`; the Claude plugin's helper is a symlink to it. Never edit it by hand. Regenerate it after changing anything under `src/ripcord`:

```bash
uv run python scripts/build_helpers.py
```

The test suite (and CI) fails when the bundled helper is out of date. Validate it through both paths with:

```bash
uv run python plugins/ripcord-resume-codex/helpers/ripcord_helper.py --help
uv run python skills/resume-claude-session/runtime/ripcord_helper.py --help
```

`build_helpers.py` concatenates the modules in dependency order, so inside `src/ripcord` import names with `from .module import name` and keep top-level names unique across modules.

## Repo Layout

- `src/ripcord`: shared recovery logic and the `ripcord.cli` entrypoint
  - `claude.py`, `codex.py`: one provider per tool — session discovery and transcript parsing
  - `recovery.py`: ranking, selection, and the bounded recovery bundle
  - `workspace.py`: exact, same-repository, and nested workspace matching
- `scripts/build_helpers.py`: generates the bundled helper
- `plugins/ripcord-resume-codex`: Claude plugin packaging and helper symlink
- `skills/resume-claude-session`: Codex skill packaging and bundled helper
- `tests`: contract-focused tests for recovery behavior and packaged helpers

## Maintenance Notes

- Regenerate the packaged helper when recovery behavior changes.
- Keep versions in `pyproject.toml`, `src/ripcord/__init__.py`, `.claude-plugin/marketplace.json`, and `plugin.json` in sync; a test enforces this.
- Keep `README.md`, `CONTRIBUTING.md`, plugin metadata, and skill docs consistent when install or usage flows change.
- Use `uv run python ...` for direct module and script execution in repo docs and contributor workflows.
