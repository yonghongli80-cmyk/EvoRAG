"""Exclusive use of the host, for measurements that talk to the model provider.

The rule is measured, not stylistic. Two host A/B runs in parallel, or an A/B run alongside a full
test suite that reaches the provider, produced a baseline side with 26 failed calls; the same command
run alone produced zero, twice. A rate-limited or contended run does not fail loudly — it produces a
report with failures in it, and the gate then refuses the *candidate* for something the environment
did. That is the same mistake as rejecting a candidate because the main tree was dirty, one layer up.

The lock is a file created with ``O_CREAT | O_EXCL``, which is atomic on every platform this runs on.
It records the pid and the time it was taken, so a run killed without cleanup does not block the next
one forever: a holder whose pid is gone, or a lock older than :data:`STALE_AFTER_S`, is taken over and
the takeover is reported rather than silently performed.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

#: A lock older than this is treated as abandoned. The longest single host A/B observed is about
#: eleven minutes, so six hours is far beyond any honest run and still short enough to notice.
STALE_AFTER_S = 6 * 60 * 60


class HostLocked(RuntimeError):
    """Another measurement holds the host; the caller must not start a second one."""


def _pid_alive(pid: int) -> bool:
    """Is ``pid`` running?

    **Windows must not use** ``os.kill(pid, 0)``: there it does not probe, it calls ``TerminateProcess``,
    so the liveness check kills the process it is asking about. Measured the hard way on 2026-10-11:
    pytest and a smoke script each terminated themselves with no output at all, because the lock they
    held carried their own pid. Windows therefore asks the process list, and any failure to ask means
    "assume alive" so that a lock is never stolen on a guess.
    """
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
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _take(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "taken_at": time.time()})
    handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(payload)


@contextmanager
def host_measurement_lock(path: str | Path):
    """Hold the host for one measurement, or refuse to start a second one.

    Yields the holder dict of a lock that was taken over from an abandoned run (empty when this run
    took a free lock). Raises :class:`HostLocked` with the holder's pid and age when someone else
    holds it, because waiting would silently queue two provider-heavy runs instead of preventing it.
    """
    lock_path = Path(path)
    takeover: dict = {}
    try:
        _take(lock_path)
    except FileExistsError:
        holder = _read_holder(lock_path)
        age = time.time() - float(holder.get("taken_at") or 0.0)
        pid = int(holder.get("pid") or 0)
        if _pid_alive(pid) and age < STALE_AFTER_S:
            raise HostLocked(
                f"host measurement already running (pid {pid}, started {int(age)}s ago); "
                "run one at a time — a contended run reports failures that look like the candidate's"
            ) from None
        lock_path.unlink(missing_ok=True)
        _take(lock_path)
        takeover = holder
    try:
        yield takeover
    finally:
        # 只删自己的锁：接管别人的锁之后，那个进程可能刚好醒来并把它删掉 ⇒ 先核对 pid。
        if int(_read_holder(lock_path).get("pid") or 0) == os.getpid():
            lock_path.unlink(missing_ok=True)


__all__ = ["STALE_AFTER_S", "HostLocked", "host_measurement_lock"]
