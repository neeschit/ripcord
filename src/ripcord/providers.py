from pathlib import Path
from typing import Any, Protocol

from .claude import ClaudeProvider
from .codex import CodexProvider
from .models import SessionDescriptor, TranscriptEvent


class Provider(Protocol):
    name: str
    home_env: str
    default_home: str
    required_markers: list[str]
    optional_markers: list[str]

    def load_sessions(self, home: Path, session_id: str | None = None) -> list[SessionDescriptor]: ...

    def parse_record(self, record: dict[str, Any]) -> list[TranscriptEvent]: ...


PROVIDERS: dict[str, Provider] = {provider.name: provider for provider in (CodexProvider(), ClaudeProvider())}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(f"unsupported provider: {name}") from None
