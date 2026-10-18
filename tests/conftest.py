from pathlib import Path

import pytest

from ripcord.recovery import RipcordRecovery
from support import RecoveryTestEnv


@pytest.fixture
def recovery_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RecoveryTestEnv:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "sessions").mkdir()
    (codex_home / "session_index.jsonl").write_text("", encoding="utf-8")

    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    (claude_home / "projects").mkdir()

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    return RecoveryTestEnv(
        root=tmp_path,
        workspace=workspace,
        codex_home=codex_home,
        claude_home=claude_home,
        recovery=RipcordRecovery(),
    )
