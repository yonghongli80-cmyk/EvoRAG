"""Emit a JSON-only inspect-only report for an explicit material snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).absolute().parent.parent
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

from rag.staging_audit import audit_staging  # noqa: E402


class _ArgumentFailure(Exception):
    pass


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentFailure(message)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="closed SQLite snapshot")
    parser.add_argument("--versions-root", required=True, type=Path, help="material versions directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _ArgumentFailure:
        report = {
            "schema_version": 1,
            "action": "inspect_only",
            "entries": [],
            "findings": [],
            "counts": {"entries": 0, "findings": 0, "by_kind": {}, "by_code": {}},
            "error": {"code": "invalid_arguments", "message": "provide only --database and --versions-root"},
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2

    report = audit_staging(args.database, args.versions_root)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 2 if "error" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())
