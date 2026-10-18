"""Generate the single-file helper bundled with the Codex skill and the Claude plugin.

The helpers run without installing the ripcord package, so this script concatenates the
modules in src/ripcord into one file. Run it after changing anything under src/ripcord:

    uv run python scripts/build_helpers.py

Pass --check to fail instead of writing when a bundled helper is out of date.
"""

import argparse
import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "ripcord"
# Dependency order: each module may only use names from modules listed before it at import time.
MODULES = ["utils", "models", "workspace", "codex", "claude", "providers", "homes", "recovery", "cli"]
# plugins/ripcord-resume-codex/helpers/ripcord_helper.py is a symlink to this file.
TARGETS = [REPO_ROOT / "skills" / "resume-claude-session" / "runtime" / "ripcord_helper.py"]
HEADER = '''#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# ///
"""Ripcord recovery helper.

Generated from src/ripcord by scripts/build_helpers.py. Do not edit by hand.
"""

import sys

if sys.version_info < (3, 14):
    import json

    print(json.dumps({"status": "error", "error": f"ripcord requires Python 3.14+; found {sys.version.split()[0]}"}))
    raise SystemExit(2)
'''


def render_helper() -> str:
    imports: set[str] = set()
    from_imports: dict[str, set[str]] = {}
    defined_in: dict[str, str] = {}
    sections: list[str] = []

    for module in MODULES:
        source = (SOURCE_ROOT / f"{module}.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        dropped_lines: set[int] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.update(_alias(alias) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.module is None:
                    raise SystemExit(f"{module}.py: use 'from .module import name' so the helper can be concatenated")
                if not node.level and node.module != "__future__":
                    from_imports.setdefault(node.module or "", set()).update(_alias(alias) for alias in node.names)
            elif not (node is tree.body[0] and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
                for name in _defined_names(node):
                    if name in defined_in:
                        raise SystemExit(f"{name} is defined in both {defined_in[name]}.py and {module}.py")
                    defined_in[name] = module
                continue
            dropped_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

        body = "\n".join(line for number, line in enumerate(source.splitlines(), start=1) if number not in dropped_lines)
        sections.append(f"# --- ripcord/{module}.py ---\n\n{body.strip()}\n")

    import_lines = sorted(f"import {name}" for name in imports if name != "sys")
    import_lines += sorted(f"from {module} import {', '.join(sorted(names))}" for module, names in from_imports.items())
    return HEADER + "\n" + "\n".join(import_lines) + "\n\n\n" + "\n\n".join(sections)


def _alias(alias: ast.alias) -> str:
    return alias.name if alias.asname is None else f"{alias.name} as {alias.asname}"


def _defined_names(node: ast.stmt) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if a bundled helper differs from the generated output")
    args = parser.parse_args()

    expected = render_helper()
    stale = [target for target in TARGETS if not target.exists() or target.read_text(encoding="utf-8") != expected]
    if args.check:
        for target in stale:
            print(f"{target.relative_to(REPO_ROOT)} is out of date; run: uv run python scripts/build_helpers.py")
        return 1 if stale else 0
    for target in stale:
        target.write_text(expected, encoding="utf-8")
        print(f"wrote {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
