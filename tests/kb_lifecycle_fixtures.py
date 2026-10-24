"""Owned subprocess helpers for KB lifecycle integration tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def child_env(
    db_path: Path,
    qdrant_path: Path,
    versions_root: Path,
    *,
    wiki_db_path: Path | None = None,
    bm25_db_path: Path | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "RAG_USERS_DB_PATH": str(db_path),
            "RAG_QDRANT_PATH": str(qdrant_path),
            "RAG_QDRANT_URL": "",
            "RAG_DOCUMENT_VERSIONS_ROOT": str(versions_root),
            "RAG_STRUCTURED_DATASETS_ROOT": str(versions_root.parent / "datasets"),
        }
    )
    if wiki_db_path is not None:
        env["RAG_WIKI_DB_PATH"] = str(wiki_db_path)
    if bm25_db_path is not None:
        env["RAG_BM25_DB_PATH"] = str(bm25_db_path)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    return env


def start_gate_child(
    db_path: Path, qdrant_path: Path, versions_root: Path, kb_id: str, *, hold: bool
) -> subprocess.Popen[str]:
    """Spawn a child that acquires the real file gate and optionally waits for release."""
    script = r'''
import json, sys
from rag.user_db import UserDB
from rag.kb_lifecycle import KBBusy, kb_mutation

db = UserDB(sys.argv[1])
kb = sys.argv[2]
hold = sys.argv[3] == "hold"
try:
    try:
        with kb_mutation(db, kb):
            print(json.dumps({"state": "acquired", "pid": __import__("os").getpid()}), flush=True)
            if hold:
                sys.stdin.readline()
    except KBBusy:
        print(json.dumps({"state": "busy", "pid": __import__("os").getpid()}), flush=True)
finally:
    db.close()
'''
    return subprocess.Popen(
        [sys.executable, "-c", script, str(db_path), kb_id, "hold" if hold else "attempt"],
        cwd=REPO_ROOT,
        env=child_env(db_path, qdrant_path, versions_root),
        stdin=subprocess.PIPE if hold else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def crash_during_delete(
    db_path: Path,
    qdrant_path: Path,
    versions_root: Path,
    kb_id: str,
    files: list[Path],
    checkpoint: str,
) -> subprocess.Popen[str]:
    """Exit from an owned child at a durable deletion checkpoint, releasing OS locks."""
    script = r'''
import os, sys
from pathlib import Path
from rag.user_db import UserDB
from rag.kb_lifecycle import kb_mutation
from rag.knowledge_base import KnowledgeBaseManager

db_path, kb_id, versions_root, checkpoint, *file_args = sys.argv[1:]
db = UserDB(db_path)
with kb_mutation(db, kb_id, mode="delete") as permit:
    token = permit.begin_delete()
    print("deleting:" + token, flush=True)
    if checkpoint == "deleting_committed":
        os._exit(70)
    manager = KnowledgeBaseManager()
    manager.delete_kb(kb_id)
    print("vectors_removed", flush=True)
    if checkpoint == "vectors_removed":
        os._exit(71)
    if checkpoint == "part_files_removed":
        Path(file_args[0]).unlink(missing_ok=True)
        print("partial_files_removed", flush=True)
        os._exit(72)
    for name in file_args:
        Path(name).unlink(missing_ok=True)
    print("files_removed_before_sql", flush=True)
    os._exit(73)
'''
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(db_path),
            kb_id,
            str(versions_root),
            checkpoint,
            *(str(path) for path in files),
        ],
        cwd=REPO_ROOT,
        env=child_env(db_path, qdrant_path, versions_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def crash_coordinator_after_cleanup(
    db_path: Path,
    qdrant_path: Path,
    versions_root: Path,
    kb_files_root: Path,
    datasets_root: Path,
    wiki_db_path: Path,
    bm25_db_path: Path,
    kb_id: str,
    actor_id: int,
    exit_code: int,
) -> subprocess.Popen[str]:
    """Crash an owned child after the real KB deletion coordinator removes files."""
    script = r'''
import offline_network_guard
import os, sys
from pathlib import Path
from rag.user_db import UserDB
import rag.routers.knowledge as knowledge

db_path, kb_id, actor_id, versions_root, kb_files_root, datasets_root, exit_code = sys.argv[1:]
db = UserDB(db_path)
real_cleanup = knowledge.cleanup_kb_files

def crash_after_cleanup(plan):
    real_cleanup(plan)
    print("actual_coordinator_cleanup_complete", flush=True)
    os._exit(int(exit_code))

knowledge.cleanup_kb_files = crash_after_cleanup
actor = {"id": int(actor_id), "username": "coordinator-test-owner", "is_admin": False}
try:
    knowledge.coordinate_kb_deletion(
        db,
        kb_id,
        actor,
        Path(versions_root),
        Path(kb_files_root),
        Path(datasets_root),
    )
finally:
    db.close()
'''
    env = child_env(
        db_path,
        qdrant_path,
        versions_root,
        wiki_db_path=wiki_db_path,
        bm25_db_path=bm25_db_path,
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(db_path),
            kb_id,
            str(actor_id),
            str(versions_root),
            str(kb_files_root),
            str(datasets_root),
            str(exit_code),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
