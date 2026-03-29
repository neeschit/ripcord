from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def expand_path(value: str | Path) -> Path:
    return Path(value).expanduser()


def canonicalize_path(value: str | Path | None) -> str | None:
    if value is None:
        return None
    try:
        return str(expand_path(value).resolve())
    except FileNotFoundError:
        return str(expand_path(value).absolute())


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def epoch_to_iso(value: int | float | None) -> str:
    if value is None:
        return ""
    if value > 10_000_000_000:
        value = value / 1000
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit <= 3:
        return text[:limit], True
    return text[: limit - 3] + "...", True


