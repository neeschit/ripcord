from pathlib import Path

from support import RecoveryTestEnv, create_codex_session


def make_repo_with_worktree(root: Path) -> tuple[Path, Path]:
    main = root / "main"
    worktree_gitdir = main / ".git" / "worktrees" / "wt"
    worktree_gitdir.mkdir(parents=True)
    (main / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (worktree_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = root / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {worktree_gitdir}\n", encoding="utf-8")
    return main, worktree


def test_session_from_another_worktree_is_a_same_repo_match(recovery_env: RecoveryTestEnv) -> None:
    main, worktree = make_repo_with_worktree(recovery_env.root / "repo")
    elsewhere = recovery_env.root / "elsewhere"
    elsewhere.mkdir()
    create_codex_session(recovery_env, session_id="main-session", updated_epoch=100, cwd=main)
    create_codex_session(recovery_env, session_id="unrelated-session", updated_epoch=200, cwd=elsewhere)

    result = recovery_env.recovery.discover(str(worktree), source_provider="codex")

    assert result["status"] == "needs_selection"
    assert [(candidate["session_id"], candidate.get("workspace_match")) for candidate in result["candidates"]] == [
        ("main-session", "same_repo"),
        ("unrelated-session", "unrelated"),
    ]


def test_session_in_a_subdirectory_is_a_nested_match(recovery_env: RecoveryTestEnv) -> None:
    subdirectory = recovery_env.workspace / "pkg"
    subdirectory.mkdir()
    create_codex_session(recovery_env, session_id="nested-session", updated_epoch=100, cwd=subdirectory)

    result = recovery_env.recovery.discover(str(recovery_env.workspace), source_provider="codex")

    assert result["candidates"][0]["workspace_match"] == "nested"
