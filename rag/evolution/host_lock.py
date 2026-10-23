"""Exclusive, recoverable leases for measurements that use the model provider.

Two concurrent host measurements have produced provider failures that looked like candidate
regressions. The lease uses an operating-system mutex on a stable sidecar file and keeps readable
ownership metadata in the requested path. The kernel mutex protects metadata creation, takeover,
and release, so an empty file or old timestamp cannot displace an active owner.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import portalocker

# A measurement normally lasts minutes; six hours is far beyond any honest run.
STALE_AFTER_S = 6 * 60 * 60


class HostLocked(RuntimeError):
    """Another measurement holds the host; the caller must not start a second one."""


def _pid_alive(pid: int) -> bool:
    """Check process liveness without risking terminating it on Windows."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        return str(int(pid)) in (listing.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _read_holder(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _take(path: Path, holder: dict | None = None) -> None:
    """Publish lease metadata while the separate kernel mutex is held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = holder or {"pid": os.getpid(), "taken_at": time.time()}
    with path.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(owner))
        stream.flush()
        os.fsync(stream.fileno())


def _busy_message(holder: dict) -> str:
    age = max(0.0, time.time() - float(holder.get("taken_at") or time.time()))
    pid = int(holder.get("pid") or 0)
    detail = f"pid {pid}, started {int(age)}s ago" if pid else "owner metadata is being published"
    return (
        f"host measurement already running ({detail}); run one at a time — "
        "a contended run reports failures that look like the candidate's"
    )


@contextmanager
def host_measurement_lock(path: str | Path):
    """Hold one host measurement lease, or raise :class:`HostLocked` immediately.

    The yielded dict describes recovered orphan metadata, and is empty when the lease was free.
    ``<path>.kernel`` is a stable sidecar and intentionally remains after release: unlinking an
    inode that another process may have opened can split the mutex into two independent locks.
    """
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    mutex_path = lock_path.with_name(f"{lock_path.name}.kernel")
    mutex = mutex_path.open("a+b")
    acquired = False
    try:
        try:
            portalocker.lock(mutex, portalocker.LOCK_EX | portalocker.LOCK_NB)
            acquired = True
        except portalocker.exceptions.LockException:
            raise HostLocked(_busy_message(_read_holder(lock_path))) from None

        previous = _read_holder(lock_path)
        takeover: dict = {}
        if previous:
            age = time.time() - float(previous.get("taken_at") or 0.0)
            pid = int(previous.get("pid") or 0)
            # With the kernel lock held, even an ancient-looking lease is inactive.
            # Retain the old stale-orphan reporting contract and never use age alone
            # to bypass a kernel lock owned by another run.
            if _pid_alive(pid) and age < STALE_AFTER_S:
                raise HostLocked(_busy_message(previous)) from None
            takeover = previous

        owner = {"pid": os.getpid(), "taken_at": time.time(), "token": uuid.uuid4().hex}
        try:
            _take(lock_path, owner)
            yield takeover
        finally:
            # Only this mutex owner can modify the lease path; token comparison also
            # protects against accidental external replacement during the lease.
            current = _read_holder(lock_path)
            if current.get("token") == owner["token"]:
                lock_path.unlink(missing_ok=True)
    finally:
        if acquired:
            try:
                portalocker.unlock(mutex)
            finally:
                mutex.close()
        else:
            mutex.close()


__all__ = ["STALE_AFTER_S", "HostLocked", "host_measurement_lock"]
