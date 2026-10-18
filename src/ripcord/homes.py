import os
from pathlib import Path

from .models import HomeStatus, HomesReport
from .providers import PROVIDERS
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
    workspace = expand_path(workspace_root or os.getcwd())
    homes = {
        name: _validate_home(
            expand_path(os.environ.get(provider.home_env, provider.default_home)),
            required_markers=provider.required_markers,
            optional_markers=provider.optional_markers,
        )
        for name, provider in PROVIDERS.items()
    }

    return HomesReport(
        homes=homes,
        workspace_root=canonicalize_path(workspace) or str(workspace),
        warnings=[warning for status in homes.values() for warning in status.warnings],
    )
