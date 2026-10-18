import json
import shutil
import subprocess
import sys
import tomllib

import pytest

import ripcord
from support import REPO_ROOT, RecoveryTestEnv, create_claude_session, create_codex_session


HELPERS = [
    "plugins/ripcord-resume-codex/helpers/ripcord_helper.py",
    "skills/resume-claude-session/runtime/ripcord_helper.py",
]


def test_bundled_helpers_match_source() -> None:
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_helpers.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("helper_path", "source_provider", "session_id"),
    [
        (HELPERS[0], "codex", "codex-helper"),
        (HELPERS[1], "claude", "claude-helper"),
    ],
)
def test_packaged_helpers_can_recover_context(
    recovery_env: RecoveryTestEnv,
    helper_path: str,
    source_provider: str,
    session_id: str,
) -> None:
    if source_provider == "codex":
        create_codex_session(recovery_env, session_id=session_id, updated_epoch=200)
    else:
        create_claude_session(recovery_env, session_id=session_id, updated_at="2026-03-29T12:00:00Z")

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / helper_path),
            "recover",
            "--source",
            source_provider,
            "--workspace",
            str(recovery_env.workspace),
            "--session-id",
            session_id,
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    payload = json.loads(completed.stdout)
    assert payload["status"] == "ready"
    assert payload["session_id"] == session_id


def find_old_python() -> str | None:
    for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3.9", "/usr/bin/python3"):
        path = shutil.which(name)
        if path is None:
            continue
        version = subprocess.run(
            [path, "-c", "import sys; print(sys.version_info >= (3, 14))"],
            capture_output=True,
            text=True,
        )
        if version.stdout.strip() == "False":
            return path
    return None


def test_helper_reports_unsupported_python() -> None:
    old_python = find_old_python()
    if old_python is None:
        pytest.skip("no Python older than 3.14 available")

    completed = subprocess.run([old_python, str(REPO_ROOT / HELPERS[0]), "--help"], capture_output=True, text=True)

    assert completed.returncode == 2, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "error"
    assert "3.14" in payload["error"]


def test_versions_agree_across_packaging() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    marketplace = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    plugin = json.loads(
        (REPO_ROOT / "plugins" / "ripcord-resume-codex" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    version = pyproject["project"]["version"]

    assert {
        ripcord.__version__,
        marketplace["metadata"]["version"],
        *(entry["version"] for entry in marketplace["plugins"]),
        plugin["version"],
    } == {version}
    assert plugin["author"]["email"] == marketplace["owner"]["email"]
