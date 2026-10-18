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
class TranscriptEvent:
    kind: str
    text: str | None = None
    name: str | None = None
    arguments: Any = None
    call_id: str | None = None
    is_command: bool = False


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
    workspace_match: str
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifact_refs: list[ArtifactRef] = field(default_factory=list)

    def to_candidate(self, score: int, workspace_match: str, workspace_reason: str) -> CandidateSession:
        return CandidateSession(
            provider=self.provider,
            session_id=self.session_id,
            workspace_root=self.workspace_root,
            updated_at=self.updated_at,
            git_branch=self.git_branch,
            git_sha=self.git_sha,
            score=score,
            preview=self.preview,
            reasons=[workspace_reason, *self.reasons],
            exact_workspace_match=workspace_match == "exact",
            workspace_match=workspace_match,
            artifact_refs=list(self.artifact_refs),
            warnings=list(self.warnings),
        )


@dataclass(slots=True)
class HomesReport:
    homes: dict[str, HomeStatus]
    workspace_root: str
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            **{f"{name}_home": status.to_dict() for name, status in self.homes.items()},
            "workspace_root": self.workspace_root,
            "warnings": list(self.warnings),
        }
