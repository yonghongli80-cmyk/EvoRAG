from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
from typing import Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.backups.models import BackupContext, BackupError, context_from_settings
from rag.backups.service import (
    create_backup,
    create_if_due,
    list_verified_backups,
    restore_test,
    verify_backup,
)


_BACKUP_ID_PATTERN = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{12}\Z")


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BackupError("invalid_cli_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="backup.py")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument(
        "--trigger", choices=("manual", "scheduled", "deploy"), default="manual"
    )

    create_due = commands.add_parser("create-if-due")
    create_due.add_argument(
        "--trigger", choices=("manual", "scheduled", "deploy"), default="scheduled"
    )

    verify = commands.add_parser("verify")
    verify.add_argument("backup_id")

    restore = commands.add_parser("restore-test")
    restore.add_argument("backup_id")
    restore.add_argument("isolation_root")

    commands.add_parser("list")
    return parser


def _write_json(payload: object, stream) -> None:
    stream.write(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    )


def _resolve_backup_id(context: BackupContext, backup_id: str) -> Path:
    if not _BACKUP_ID_PATTERN.fullmatch(backup_id):
        raise BackupError("unsafe_backup_id")
    root = context.backup_root.resolve()
    candidate = (root / backup_id).resolve()
    if candidate.parent != root:
        raise BackupError("unsafe_backup_id")
    return candidate


def _record_payload(record) -> dict[str, object]:
    payload: dict[str, object] = {
        "backup_id": record.backup_id,
        "database_count": record.database_count,
        "file_count": record.file_count,
        "status": record.status,
        "vector_rebuild_required": record.vector_rebuild_required,
    }
    if hasattr(record, "created_at"):
        payload["created_at"] = record.created_at
    if hasattr(record, "total_bytes"):
        payload["total_bytes"] = record.total_bytes
    return payload


def main(
    argv: list[str] | None = None,
    context_factory: Callable[[], BackupContext] = context_from_settings,
    now: Callable[[], float] = time.time,
) -> int:
    try:
        arguments = _parser().parse_args(argv)
        context = context_factory()

        if arguments.command == "create":
            result = create_backup(context, arguments.trigger, now=now())
            _write_json(_record_payload(result), sys.stdout)
        elif arguments.command == "create-if-due":
            result = create_if_due(context, arguments.trigger, now=now())
            if result is None:
                _write_json({"status": "reused"}, sys.stdout)
            else:
                _write_json(_record_payload(result), sys.stdout)
        elif arguments.command == "verify":
            result = verify_backup(
                _resolve_backup_id(context, arguments.backup_id)
            )
            _write_json(_record_payload(result), sys.stdout)
        elif arguments.command == "restore-test":
            backup_dir = _resolve_backup_id(context, arguments.backup_id)
            protected_roots = (
                context.project_root,
                context.data_root,
                context.backup_root,
                *context.file_roots.values(),
                *(path.parent for path in context.databases.values()),
            )
            result = restore_test(
                backup_dir,
                Path(arguments.isolation_root),
                protected_roots=tuple(protected_roots),
            )
            _write_json(_record_payload(result), sys.stdout)
        elif arguments.command == "list":
            records = list_verified_backups(context.backup_root)
            _write_json(
                {
                    "backups": [_record_payload(record) for record in records],
                    "status": "ok",
                },
                sys.stdout,
            )
        else:
            raise BackupError("invalid_cli_arguments")
        return 0
    except BackupError as error:
        _write_json({"code": error.code, "status": "error"}, sys.stderr)
        return 1
    except Exception:
        _write_json({"code": "internal_error", "status": "error"}, sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
