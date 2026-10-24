"""Durable KB deletion state and synchronous per-KB mutation admission."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import portalocker


class KBLifecycleError(RuntimeError):
    """Base domain error with a stable machine-readable code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code.replace("_", " "))


class KBBusy(KBLifecycleError):
    def __init__(self, code: str = "knowledge_base_busy") -> None:
        super().__init__(code, "知识库正在处理中，请稍后重试。")


class KBDeleting(KBLifecycleError):
    def __init__(self, code: str = "knowledge_base_deleting") -> None:
        super().__init__(code, "知识库正在删除，当前操作无法继续。")


class KBDeleted(KBLifecycleError):
    def __init__(self, code: str = "knowledge_base_deleted") -> None:
        super().__init__(code, "知识库已删除。")


class KBUnknown(KBLifecycleError):
    def __init__(self, code: str = "knowledge_base_unknown") -> None:
        super().__init__(code, "知识库不存在。")


class KBDeleteIncomplete(KBLifecycleError):
    def __init__(self, code: str = "knowledge_base_delete_incomplete") -> None:
        super().__init__(code, "知识库删除未完成，请重试。")


@dataclass
class _Owner:
    permit: KBMutationPermit
    pid: int
    thread_id: int
    task: asyncio.Task | None
    depth: int = 1
    active: bool = True


_local_locks: dict[tuple[object, str], threading.Lock] = {}
_local_locks_guard = threading.Lock()
_owners: contextvars.ContextVar[dict[tuple[object, str], _Owner] | None] = contextvars.ContextVar(
    "kb_mutation_owners", default=None
)


def _async_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _lock_key(db: Any, kb_id: str) -> tuple[object, str]:
    if str(db.db_path) == ":memory:":
        database: object = ("memory", os.getpid(), id(db.cm))
    else:
        database = os.path.normcase(str(Path(db.db_path).expanduser().resolve(strict=False)))
    return database, kb_id


def _process_lock_path(db: Any, kb_id: str) -> Path | None:
    if str(db.db_path) == ":memory:":
        return None
    database = Path(db.db_path).expanduser().resolve(strict=False)
    digest = hashlib.sha256(kb_id.encode("utf-8")).hexdigest()
    return database.with_name(f"{database.name}.kb_lifecycle_locks") / f"{digest}.lock"


def _thread_lock(key: tuple[object, str]) -> threading.Lock:
    with _local_locks_guard:
        lock = _local_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _local_locks[key] = lock
        return lock


def assert_kb_available(db: Any, kb_id: str, *, allow_deleting: bool = False) -> None:
    """Reject a lifecycle tombstone; this check does not grant authorization."""
    row = db.get_kb_lifecycle(kb_id)
    if row is None:
        return
    if row["state"] == "deleted":
        raise KBDeleted()
    if row["state"] == "deleting" and not allow_deleting:
        raise KBDeleting()


def assert_kb_available_in_tx(
    conn: sqlite3.Connection, kb_id: str, *, allow_deleting: bool = False
) -> None:
    """Check lifecycle state using the caller's active SQLite transaction."""
    row = conn.execute("SELECT state FROM kb_lifecycle WHERE kb_id = ?", (kb_id,)).fetchone()
    if row is None:
        return
    state = row["state"] if isinstance(row, sqlite3.Row) else row[0]
    if state == "deleted":
        raise KBDeleted()
    if state == "deleting" and not allow_deleting:
        raise KBDeleting()


class KBMutationPermit:
    """Permit bound to one owned synchronous admission and its delete token."""

    def __init__(self, db: Any, kb_id: str, mode: str) -> None:
        self.db = db
        self.kb_id = kb_id
        self.mode = mode
        self._delete_token: str | None = None

    def _assert_active_owner(self) -> None:
        """Reject stale permits and use outside their acquiring execution context."""
        owner = (_owners.get() or {}).get(_lock_key(self.db, self.kb_id))
        if owner is None or owner.permit is not self or not owner.active:
            raise KBLifecycleError("kb_mutation_permit_inactive")
        if (
            owner.pid != os.getpid()
            or owner.thread_id != threading.get_ident()
            or owner.task is not _async_task()
        ):
            raise KBLifecycleError("kb_mutation_permit_owner_mismatch")

    def begin_delete(self) -> str:
        self._assert_active_owner()
        if self.mode != "delete":
            raise KBLifecycleError("delete_permit_required")
        if self.kb_id == "rag_docs" or self.db.get_kb_metadata(self.kb_id) is None:
            raise KBUnknown()

        with self.db.locked_transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT state, delete_token FROM kb_lifecycle WHERE kb_id = ?",
                (self.kb_id,),
            ).fetchone()
            if row is not None:
                if row["state"] == "deleted":
                    raise KBDeleted()
                token = row["delete_token"]
                if not token:
                    raise KBDeleteIncomplete()
                self._delete_token = token
                conn.execute(
                    "UPDATE kb_lifecycle SET last_error_code = NULL "
                    "WHERE kb_id = ? AND state = 'deleting' AND delete_token = ?",
                    (self.kb_id, token),
                )
                return token

            token = uuid.uuid4().hex
            cursor = conn.execute(
                "INSERT OR IGNORE INTO kb_lifecycle "
                "(kb_id, state, delete_token, generation, requested_at) "
                "VALUES (?, 'deleting', ?, 1, ?)",
                (self.kb_id, token, time.time()),
            )
            if cursor.rowcount != 1:
                raise KBDeleteIncomplete()
            self._delete_token = token
            return token

    def fail_delete(self, code: str) -> None:
        self._assert_active_owner()
        if self.mode != "delete" or self._delete_token is None:
            raise KBDeleteIncomplete()
        with self.db.locked_transaction(immediate=True) as conn:
            cursor = conn.execute(
                "UPDATE kb_lifecycle SET last_error_code = ? "
                "WHERE kb_id = ? AND state = 'deleting' AND delete_token = ?",
                (code, self.kb_id, self._delete_token),
            )
            if cursor.rowcount != 1:
                raise KBDeleteIncomplete("delete_token_mismatch")

    def finish_delete(self, cleanup_sql: Callable[[sqlite3.Connection], None]) -> None:
        self._assert_active_owner()
        if self.mode != "delete" or self._delete_token is None:
            raise KBDeleteIncomplete()
        with self.db.locked_transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT state, delete_token FROM kb_lifecycle WHERE kb_id = ?",
                (self.kb_id,),
            ).fetchone()
            if row is None or row["state"] != "deleting" or row["delete_token"] != self._delete_token:
                raise KBDeleteIncomplete("delete_token_mismatch")
            cleanup_sql(conn)
            cursor = conn.execute(
                "UPDATE kb_lifecycle SET state = 'deleted', deleted_at = ?, last_error_code = NULL "
                "WHERE kb_id = ? AND state = 'deleting' AND delete_token = ?",
                (time.time(), self.kb_id, self._delete_token),
            )
            if cursor.rowcount != 1:
                raise KBDeleteIncomplete("delete_token_mismatch")


@contextmanager
def kb_mutation(db: Any, kb_id: str, *, mode: str = "write") -> Iterator[KBMutationPermit]:
    """Acquire nonblocking same-KB admission for the actual synchronous worker."""
    if mode not in {"write", "delete"}:
        raise ValueError("mode must be 'write' or 'delete'")
    if mode == "delete" and kb_id == "rag_docs":
        raise KBUnknown()
    if _async_task() is not None:
        raise KBLifecycleError(
            "kb_mutation_requires_sync_worker",
            "知识库写入准入必须在同步工作线程中获取。",
        )

    key = _lock_key(db, kb_id)
    current = _owners.get() or {}
    owner = current.get(key)
    pid, thread_id = os.getpid(), threading.get_ident()
    if owner is not None:
        if owner.pid != pid or owner.thread_id != thread_id or owner.task is not None:
            raise KBBusy()
        if mode == "delete" and owner.permit.mode != "delete":
            raise KBLifecycleError("delete_permit_required")
        owner.depth += 1
        try:
            assert_kb_available(db, kb_id, allow_deleting=mode == "delete")
            yield owner.permit
        finally:
            owner.depth -= 1
        return

    thread_lock = _thread_lock(key)
    if not thread_lock.acquire(blocking=False):
        raise KBBusy()

    lock_stream = None
    file_locked = False
    try:
        lock_path = _process_lock_path(db, kb_id)
        if lock_path is not None:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_stream = lock_path.open("a+b")
            try:
                portalocker.lock(lock_stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
                file_locked = True
            except portalocker.exceptions.LockException:
                raise KBBusy() from None

        assert_kb_available(db, kb_id, allow_deleting=mode == "delete")
        permit = KBMutationPermit(db, kb_id, mode)
        owner = _Owner(permit, pid, thread_id, _async_task())
        next_owners = dict(current)
        next_owners[key] = owner
        context_token = _owners.set(next_owners)
        try:
            yield permit
        finally:
            owner.active = False
            _owners.reset(context_token)
    finally:
        if file_locked and lock_stream is not None:
            try:
                portalocker.unlock(lock_stream)
            finally:
                lock_stream.close()
        elif lock_stream is not None:
            lock_stream.close()
        thread_lock.release()


__all__ = [
    "KBLifecycleError",
    "KBBusy",
    "KBDeleting",
    "KBDeleted",
    "KBUnknown",
    "KBDeleteIncomplete",
    "KBMutationPermit",
    "kb_mutation",
    "assert_kb_available",
    "assert_kb_available_in_tx",
]
