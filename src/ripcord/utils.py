import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TextIO

try:
    from compression import zstd
except ImportError:  # Python built without libzstd
    zstd = None


# Harness-injected context arrives as a single block wrapped in a tag like
# <system-reminder> or <environment_context>; the hyphen/underscore keeps pasted HTML out.
WRAPPED_BLOCK = re.compile(r"\A<([A-Za-z][A-Za-z0-9]*[-_][\w-]*)>.*</\1>\Z", re.DOTALL)


def expand_path(value: str | Path) -> Path:
    return Path(value).expanduser()


def canonicalize_path(value: str | Path | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        return str(expand_path(value).resolve())
    except FileNotFoundError:
        return str(expand_path(value).absolute())


def open_text(path: Path) -> TextIO:
    if path.suffix == ".zst":
        if zstd is None:
            raise OSError(f"cannot read {path}: this Python has no zstd support")
        return zstd.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def find_jsonl(path: Path) -> Path:
    """Return path, or its zstd-compressed sibling when only that exists (Codex compresses old rollouts)."""
    compressed = path.with_name(path.name + ".zst")
    if not path.exists() and zstd is not None and compressed.exists():
        return compressed
    return path


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with open_text(path) as handle:
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


def read_tail_records(path: Path, max_bytes: int = 65536) -> list[dict[str, Any]]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(size - max_bytes, 0))
        chunk = handle.read()
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes:
        lines = lines[1:]
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def epoch_to_iso(value: int | float | None) -> str:
    if value is None:
        return ""
    if value > 10_000_000_000:
        value = value / 1000
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def mtime_to_iso(path: Path) -> str:
    return epoch_to_iso(path.stat().st_mtime)


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit <= 3:
        return text[:limit], True
    return text[: limit - 3] + "...", True


def compact_preview(text: str) -> str:
    preview, _ = truncate_text(" ".join(text.split()), 160)
    return preview


def is_wrapped_block(text: str) -> bool:
    return WRAPPED_BLOCK.match(text.strip()) is not None
