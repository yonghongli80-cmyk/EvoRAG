from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from rag.backups.models import context_from_settings
from rag.backups.service import verify_backup
from rag.test_artifact_cleanup import apply_cleanup, find_candidates
from rag.user_db import UserDB

_BACKUP_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{12}\Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verified-backup-id")
    args = parser.parse_args(argv)

    db = UserDB(settings.users_db_path)
    try:
        candidates = find_candidates(db)
        payload: dict[str, object] = {
            "status": "dry-run",
            "user_ids": candidates.user_ids,
            "usernames": candidates.usernames,
            "knowledge_base_ids": candidates.kb_ids,
        }
        if args.apply:
            if not args.verified_backup_id or not _BACKUP_ID.fullmatch(args.verified_backup_id):
                raise ValueError("a verified backup id is required")
            context = context_from_settings()
            verification = verify_backup(context.backup_root / args.verified_backup_id)
            if verification.status != "verified":
                raise ValueError("backup is not verified")
            payload.update(apply_cleanup(db, candidates))
            payload["status"] = "applied"
            payload["backup_id"] = args.verified_backup_id
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "detail": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
