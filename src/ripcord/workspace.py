from pathlib import Path

from .utils import canonicalize_path


WORKSPACE_MATCH_SCORES = {
    "exact": 1000,
    "same_repo": 500,
    "nested": 400,
    "unrelated": 0,
}
WORKSPACE_MATCH_REASONS = {
    "exact": "exact workspace match",
    "same_repo": "same git repository (another worktree or subdirectory)",
    "nested": "parent or child directory of the workspace",
    "unrelated": "different workspace",
}


class WorkspaceMatcher:
    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = canonicalize_path(workspace_root) or workspace_root
        self._git_dirs: dict[str, str | None] = {}
        self._workspace_git_dir = self._git_common_dir(self.workspace_root)

    def relation(self, cwd: str) -> str:
        candidate = canonicalize_path(cwd)
        if candidate is None:
            return "unrelated"
        if candidate == self.workspace_root:
            return "exact"
        if self._workspace_git_dir is not None and self._git_common_dir(candidate) == self._workspace_git_dir:
            return "same_repo"
        workspace, other = Path(self.workspace_root), Path(candidate)
        if workspace in other.parents or other in workspace.parents:
            return "nested"
        return "unrelated"

    def _git_common_dir(self, path: str) -> str | None:
        if path not in self._git_dirs:
            self._git_dirs[path] = _find_git_common_dir(Path(path))
        return self._git_dirs[path]


def _find_git_common_dir(path: Path) -> str | None:
    for directory in (path, *path.parents):
        dot_git = directory / ".git"
        try:
            if dot_git.is_dir():
                return str(dot_git.resolve())
            if dot_git.is_file():
                return _resolve_gitdir_file(directory, dot_git)
        except OSError:
            return None
    return None


def _resolve_gitdir_file(directory: Path, dot_git: Path) -> str | None:
    content = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    if not content.startswith("gitdir:"):
        return None
    gitdir = Path(content.removeprefix("gitdir:").strip())
    if not gitdir.is_absolute():
        gitdir = directory / gitdir
    commondir_file = gitdir / "commondir"
    if commondir_file.is_file():
        common = Path(commondir_file.read_text(encoding="utf-8").strip())
        gitdir = common if common.is_absolute() else gitdir / common
    return str(gitdir.resolve())
