from __future__ import annotations

from contextlib import closing
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .models import ArtifactRef, CandidateSession
from .utils import canonicalize_path, epoch_to_iso, iter_jsonl, truncate_text


@dataclass(slots=True)
class SessionDescriptor:
    provider: str
    session_id: str
    workspace_root: str
    updated_at: str
    git_branch: str | None
    git_sha: str | None
    preview: str
    completeness_score: int
    enrichment_score: int
    exact_workspace_match: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifact_refs: list[ArtifactRef] = field(default_factory=list)

    def to_candidate(self, score: int) -> CandidateSession:
        return CandidateSession(
            provider=self.provider,
            session_id=self.session_id,
            workspace_root=self.workspace_root,
            updated_at=self.updated_at,
            git_branch=self.git_branch,
            git_sha=self.git_sha,
            score=score,
            preview=self.preview,
            reasons=list(self.reasons),
            exact_workspace_match=self.exact_workspace_match,
            artifact_refs=list(self.artifact_refs),
            warnings=list(self.warnings),
        )


def _workspace_reason(exact_match: bool) -> str:
    return "exact workspace match" if exact_match else "non-exact workspace match"


def load_codex_sessions(codex_home: Path, workspace_root: str | None = None, session_id: str | None = None) -> list[SessionDescriptor]:
    canonical_workspace = canonicalize_path(workspace_root)
    rows = _read_codex_threads(codex_home, session_id=session_id)
    results: list[SessionDescriptor] = []
    for row in rows:
        current_session_id = row["id"]
        if session_id and current_session_id != session_id:
            continue

        cwd = canonicalize_path(row.get("cwd")) or ""
        exact_match = canonical_workspace is not None and cwd == canonical_workspace
        rollout_path = row.get("rollout_path") or ""
        transcript_path = Path(rollout_path) if rollout_path else None
        transcript_exists = transcript_path is not None and transcript_path.exists()
        warnings: list[str] = []
        if not transcript_exists:
            warnings.append(f"missing primary transcript: {rollout_path or '<unknown>'}")

        preview = row.get("first_user_message") or row.get("title") or current_session_id
        preview, _ = truncate_text(preview.strip(), 160)

        artifact_refs: list[ArtifactRef] = []
        artifact_refs.append(
            ArtifactRef(
                provider="codex",
                session_id=current_session_id,
                kind="transcript",
                path=str(transcript_path) if transcript_path is not None else "",
                label="Codex rollout transcript",
                is_primary=True,
            )
        )

        shell_glob = codex_home / "shell_snapshots"
        shell_refs = list(sorted(shell_glob.glob(f"{current_session_id}.*.sh"))) if shell_glob.exists() else []
        for snap in shell_refs:
            artifact_refs.append(
                ArtifactRef(
                    provider="codex",
                    session_id=current_session_id,
                    kind="shell_snapshot",
                    path=str(snap),
                    label=f"Shell snapshot {snap.name}",
                )
            )

        reasons = [_workspace_reason(exact_match)]
        if row.get("git_branch"):
            reasons.append("git branch metadata present")
        if row.get("git_sha"):
            reasons.append("git sha metadata present")
        if transcript_exists:
            reasons.append("primary transcript present")
        if shell_refs:
            reasons.append("shell snapshots present")

        results.append(
            SessionDescriptor(
                provider="codex",
                session_id=current_session_id,
                workspace_root=cwd,
                updated_at=epoch_to_iso(row.get("updated_at")),
                git_branch=row.get("git_branch"),
                git_sha=row.get("git_sha"),
                preview=preview,
                completeness_score=25 if transcript_exists else 0,
                enrichment_score=len(shell_refs) * 2 + (2 if row.get("git_branch") else 0) + (2 if row.get("git_sha") else 0),
                exact_workspace_match=exact_match,
                reasons=reasons,
                warnings=warnings,
                artifact_refs=artifact_refs,
            )
        )
    return results


def _read_codex_threads(codex_home: Path, session_id: str | None = None) -> list[dict]:
    database_candidates = sorted(codex_home.glob("state_*.sqlite"))
    if not database_candidates:
        return _read_codex_from_index(codex_home)

    database_path = database_candidates[-1]
    query = """
        select id, rollout_path, updated_at, cwd, title, git_branch, git_sha, first_user_message
        from threads
        where archived = 0
    """
    params: tuple = ()
    if session_id:
        query += " and id = ?"
        params = (session_id,)
    query += " order by updated_at desc"
    try:
        with closing(sqlite3.connect(database_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
    except sqlite3.Error:
        return _read_codex_from_index(codex_home)
    return [dict(row) for row in rows]


def _read_codex_from_index(codex_home: Path) -> list[dict]:
    index_path = codex_home / "session_index.jsonl"
    if not index_path.exists():
        return []
    entries = list(iter_jsonl(index_path))
    rollouts = _map_codex_rollouts(codex_home)
    rows: list[dict] = []
    for entry in entries:
        current_session_id = entry.get("id", "")
        rows.append(
            {
                "id": current_session_id,
                "rollout_path": str(rollouts[current_session_id]) if current_session_id in rollouts else "",
                "updated_at": 0,
                "cwd": "",
                "title": entry.get("thread_name", current_session_id),
                "git_branch": None,
                "git_sha": None,
                "first_user_message": entry.get("thread_name", ""),
            }
        )
    return rows


def _map_codex_rollouts(codex_home: Path) -> dict[str, Path]:
    results: dict[str, Path] = {}
    for path in codex_home.glob("sessions/**/*.jsonl"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                first_line = handle.readline().strip()
        except OSError:
            continue
        if not first_line:
            continue
        try:
            record = json.loads(first_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload", {})
        if isinstance(payload, dict) and record.get("type") == "session_meta":
            current_session_id = payload.get("id")
            if isinstance(current_session_id, str) and current_session_id:
                results[current_session_id] = path
    return results


def load_claude_sessions(claude_home: Path, workspace_root: str | None = None, session_id: str | None = None) -> list[SessionDescriptor]:
    canonical_workspace = canonicalize_path(workspace_root)
    meta_by_id = _read_claude_meta(claude_home, session_id=session_id)
    results: list[SessionDescriptor] = []
    for transcript_path in sorted(claude_home.glob("projects/**/*.jsonl")):
        if "/subagents/" in str(transcript_path):
            continue
        current_session_id = transcript_path.stem
        if session_id and current_session_id != session_id:
            continue

        cwd, preview, updated_at, git_branch = _read_claude_transcript_metadata(transcript_path)
        exact_match = canonical_workspace is not None and canonicalize_path(cwd) == canonical_workspace
        metadata = meta_by_id.get(current_session_id, {})
        warnings: list[str] = []

        artifact_refs: list[ArtifactRef] = [
            ArtifactRef(
                provider="claude",
                session_id=current_session_id,
                kind="transcript",
                path=str(transcript_path),
                label="Claude project transcript",
                is_primary=True,
            )
        ]

        meta_path = claude_home / "usage-data" / "session-meta" / f"{current_session_id}.json"
        has_meta = meta_path.exists()
        if has_meta:
            artifact_refs.append(
                ArtifactRef(
                    provider="claude",
                    session_id=current_session_id,
                    kind="metadata",
                    path=str(meta_path),
                    label="Claude session metadata",
                )
            )

        session_dir = transcript_path.with_suffix("")
        subagent_refs = list(sorted((session_dir / "subagents").glob("*.jsonl"))) if session_dir.exists() else []
        for subagent in subagent_refs:
            artifact_refs.append(
                ArtifactRef(
                    provider="claude",
                    session_id=current_session_id,
                    kind="subagent",
                    path=str(subagent),
                    label=f"Subagent transcript {subagent.name}",
                )
            )

        file_history_root = claude_home / "file-history" / current_session_id
        has_file_history = file_history_root.exists()
        if has_file_history:
            artifact_refs.append(
                ArtifactRef(
                    provider="claude",
                    session_id=current_session_id,
                    kind="file_history",
                    path=str(file_history_root),
                    label="Claude file history directory",
                )
            )

        preview, _ = truncate_text(preview, 160)
        reasons = [_workspace_reason(exact_match)]
        reasons.append("primary transcript present")
        if git_branch:
            reasons.append("git branch metadata present")
        if has_meta:
            reasons.append("session metadata present")
        if subagent_refs:
            reasons.append("subagent transcripts present")
        if has_file_history:
            reasons.append("file history present")

        results.append(
            SessionDescriptor(
                provider="claude",
                session_id=current_session_id,
                workspace_root=canonicalize_path(cwd) or cwd,
                updated_at=updated_at or metadata.get("start_time", ""),
                git_branch=git_branch,
                git_sha=None,
                preview=preview,
                completeness_score=25,
                enrichment_score=(2 if has_meta else 0) + len(subagent_refs) * 2 + (2 if has_file_history else 0),
                exact_workspace_match=exact_match,
                reasons=reasons,
                warnings=warnings,
                artifact_refs=artifact_refs,
            )
        )
    return results


def _read_claude_meta(claude_home: Path, session_id: str | None = None) -> dict[str, dict]:
    meta_dir = claude_home / "usage-data" / "session-meta"
    if not meta_dir.exists():
        return {}
    if session_id:
        meta_file = meta_dir / f"{session_id}.json"
        if not meta_file.exists():
            return {}
        try:
            return {session_id: json.loads(meta_file.read_text(encoding="utf-8"))}
        except json.JSONDecodeError:
            return {}
    result: dict[str, dict] = {}
    for meta_file in meta_dir.glob("*.json"):
        try:
            result[meta_file.stem] = json.loads(meta_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
    return result


def _read_claude_transcript_metadata(transcript_path: Path) -> tuple[str, str, str, str | None]:
    cwd = ""
    preview = transcript_path.stem
    updated_at = ""
    git_branch = None
    for record in iter_jsonl(transcript_path):
        timestamp = record.get("timestamp")
        if timestamp:
            updated_at = timestamp
        cwd = record.get("cwd", cwd)
        git_branch = record.get("gitBranch", git_branch)
        if record.get("type") == "user":
            message = record.get("message", {})
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                preview = content.strip()
                break
    return cwd, preview, updated_at, git_branch


def iter_sessions(provider: str, home: Path, workspace_root: str | None = None, session_id: str | None = None) -> Iterable[SessionDescriptor]:
    if provider == "codex":
        return load_codex_sessions(home, workspace_root=workspace_root, session_id=session_id)
    if provider == "claude":
        return load_claude_sessions(home, workspace_root=workspace_root, session_id=session_id)
    raise ValueError(f"unsupported provider: {provider}")
