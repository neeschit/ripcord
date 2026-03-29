from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class HomeStatus:
    path: str
    exists: bool
    valid: bool
    markers_present: list[str] = field(default_factory=list)
    missing_markers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ArtifactRef:
    provider: str
    session_id: str
    kind: str
    path: str
    label: str
    is_primary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ArtifactReadHint:
    artifact_ref: ArtifactRef
    mode: str
    line_start: int | None
    line_count: int
    max_chars: int | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TranscriptHints:
    total_lines: int
    first_user_line: int | None = None
    first_user_preview: str | None = None
    last_user_line: int | None = None
    last_user_preview: str | None = None
    last_assistant_line: int | None = None
    last_assistant_preview: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CandidateSession:
    provider: str
    session_id: str
    workspace_root: str
    updated_at: str
    git_branch: str | None
    git_sha: str | None
    score: int
    preview: str
    reasons: list[str]
    exact_workspace_match: bool
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class HomesReport:
    ripcord_home: str
    codex_home: HomeStatus
    claude_home: HomeStatus
    workspace_root: str
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ripcord_home": self.ripcord_home,
            "codex_home": self.codex_home.to_dict(),
            "claude_home": self.claude_home.to_dict(),
            "workspace_root": self.workspace_root,
            "warnings": list(self.warnings),
        }
