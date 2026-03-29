from __future__ import annotations

import os
from pathlib import Path

from .models import HomeStatus, HomesReport
from .utils import canonicalize_path, expand_path


def _validate_home(path: Path, required_markers: list[str], optional_markers: list[str]) -> HomeStatus:
    exists = path.exists()
    present_required = [marker for marker in required_markers if (path / marker).exists()]
    present_optional = []
    for marker in optional_markers:
        if "*" in marker:
            if list(path.glob(marker)):
                present_optional.append(marker)
            continue
        if (path / marker).exists():
            present_optional.append(marker)
    missing_required = [marker for marker in required_markers if marker not in present_required]
    warnings: list[str] = []

    if not exists:
        warnings.append(f"{path} does not exist")
    elif missing_required:
        warnings.append(f"{path} is missing required markers: {', '.join(missing_required)}")

    return HomeStatus(
        path=canonicalize_path(path) or str(path),
        exists=exists,
        valid=exists and not missing_required,
        markers_present=present_required + present_optional,
        missing_markers=missing_required,
        warnings=warnings,
    )


def resolve_homes(workspace_root: str | None = None) -> HomesReport:
    ripcord_home = expand_path(os.environ.get("RIPCORD_HOME", "~/.ripcord"))
    codex_home = expand_path(os.environ.get("CODEX_HOME", "~/.codex"))
    claude_home = expand_path(os.environ.get("CLAUDE_HOME", "~/.claude"))
    workspace = expand_path(workspace_root or os.getcwd())

    codex_status = _validate_home(
        codex_home,
        required_markers=["auth.json", "session_index.jsonl", "sessions"],
        optional_markers=["shell_snapshots", "state_5.sqlite", "state_*.sqlite"],
    )
    claude_status = _validate_home(
        claude_home,
        required_markers=["projects"],
        optional_markers=["usage-data/session-meta", "file-history", "shell-snapshots"],
    )

    warnings = list(codex_status.warnings) + list(claude_status.warnings)

    return HomesReport(
        ripcord_home=canonicalize_path(ripcord_home) or str(ripcord_home),
        codex_home=codex_status,
        claude_home=claude_status,
        workspace_root=canonicalize_path(workspace) or str(workspace),
        warnings=warnings,
    )
