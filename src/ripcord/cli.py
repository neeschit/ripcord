from __future__ import annotations

import argparse
import json
from typing import Any

from .recovery import RipcordRecovery


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recover bounded foreign-session context for Ripcord skills.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="Find foreign sessions for a workspace.")
    discover.add_argument("--source", required=True, choices=["codex", "claude"])
    discover.add_argument("--workspace", required=True)
    discover.add_argument("--host", choices=["codex", "claude"])
    discover.add_argument("--limit", type=int, default=5)
    recover = subparsers.add_parser("recover", help="Recover a bounded foreign-session bundle.")
    recover.add_argument("--source", required=True, choices=["codex", "claude"])
    recover.add_argument("--workspace", required=True)
    recover.add_argument("--session-id")
    recover.add_argument("--host", choices=["codex", "claude"])
    recover.add_argument("--limit", type=int, default=5)

    return parser


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    recovery = RipcordRecovery()

    if args.command == "discover":
        payload = recovery.discover(
            workspace_root=args.workspace,
            source_provider=args.source,
            host_provider=args.host,
            limit=args.limit,
        )
        return _emit(payload)

    payload = recovery.recover(
        workspace_root=args.workspace,
        source_provider=args.source,
        session_id=args.session_id,
        host_provider=args.host,
        limit=args.limit,
    )
    return _emit(payload)


if __name__ == "__main__":
    raise SystemExit(main())
