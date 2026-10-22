"""Applying an approved patch and restarting the service, with the guards that make it safe.

The red line is that the system never merges anything on its own. This module does not soften it: it
acts only on a candidate an administrator has already approved, only when a human clicks, and the
click *is* that decision. What it adds is the safety a manual sequence lacks — before anything is
killed it checks that the patch applies cleanly, that the application still imports, and that the
patch's own target tests pass; if any of those fail it reverts and leaves the running service alone.

A restart cannot happen inside the request that asks for it, because the process doing the work is
the one being replaced. So the request writes the intent down and spawns a detached helper that waits
for the port to free, starts the service the same way the operator does, polls ``/health``, and — if
the service does not come back — reverts the patch and starts it again, so a bad deployment ends with
the previous code running rather than with nothing running.

Everything it learns is logged to ``artifacts/service-control.log``. The evolution audit table is not
used: its action column is CHECK-constrained to the three diagnosis decisions, and rebuilding a table
to record an operational event is not worth it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

#: Where the helper appends what it did, next to the exported patches it acts on.
LOG_NAME = "service-control.log"

#: 覆盖日志目录的环境变量：留一条缝，让"不该写进运维日志的调用方"（测试、演练）能改道。
LOG_DIR_ENV = "RAG_EVOLUTION_SERVICE_LOG_DIR"


def service_log_path(repo_root: str | Path) -> Path:
    """The one place that decides where the operational log goes.

    It used to be written inline as ``<repo>/artifacts/service-control.log`` at every call site, and
    the API tests went through those call sites — so every run of the suite appended its own lines to
    the operator's log, mixing test fixtures (``candidate 1 applied by admin``) with real operations
    (2026-10-12: reading that file during a drill, I could not tell which lines were mine and which
    came from the suite). One function, and a test can point it at a temp directory; the returned
    path is also what ``spawn_restart_helper`` hands to the detached helper, so both stay in sync.
    """
    override = os.environ.get(LOG_DIR_ENV, "").strip()
    directory = Path(override) if override else Path(repo_root) / "artifacts"
    return directory / LOG_NAME


#: The apply/rollback lease (C-11, 2026-10-22). One file next to the operational log, created with
#: the same atomic ``O_CREAT | O_EXCL`` protocol as the host A/B lock (:mod:`rag.evolution.host_lock`).
LOCK_NAME = "evolution-apply.lock"

#: How long the detached restart helper waits for the request that spawned it to release the lease.
#: The request holds it until its response is built, so a few seconds is the normal case; the wait
#: exists because the helper runs *after* the response, never during it.
HELPER_LOCK_WAIT_S = 60.0


def apply_lock_path(repo_root: str | Path) -> Path:
    """Where apply/rollback take their exclusive lease.

    **Why a separate file instead of the host A/B lock:** the two protect different things and can
    be legitimately concurrent (a host A/B measurement must not block an administrator's apply). The
    location follows :func:`service_log_path`, so the existing ``RAG_EVOLUTION_SERVICE_LOG_DIR``
    override (and the suite's fixture) moves the lease with the log and never touches the operator's
    ``artifacts/`` directory.
    """
    return service_log_path(repo_root).parent / LOCK_NAME


class _HeldLease:
    """A lease acquired by hand (the helper is not inside a ``with`` statement)."""

    def __init__(self, manager) -> None:
        self._manager = manager

    def release(self) -> None:
        self._manager.__exit__(None, None, None)


def acquire_apply_lease(
    lock_path: str | Path,
    *,
    wait_seconds: float = HELPER_LOCK_WAIT_S,
    poll_seconds: float = 0.5,
    sleep=time.sleep,
) -> _HeldLease | None:
    """Take the apply lease, waiting up to ``wait_seconds``; ``None`` when it never freed.

    The detached restart helper needs exactly this: the API request that spawned it is still
    holding the lease (it holds it through the spawn, so two applies cannot interleave), and the
    helper must take over **the same** lease before it starts stopping/starting the process. Waiting
    is bounded — a helper that never gets the lease must not restart a service it does not own.

    Re-entrancy has the same meaning as everywhere else: a process that already holds the lease gets
    ``None`` rather than a second nested hold.
    """
    from rag.evolution.host_lock import HostLocked, host_measurement_lock

    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        manager = host_measurement_lock(lock_path)
        try:
            manager.__enter__()
        except HostLocked:
            if time.monotonic() >= deadline:
                return None
            sleep(max(0.01, float(poll_seconds)))
            continue
        return _HeldLease(manager)

#: Bounds: applying and importing are fast; the target tests may not be.
GIT_TIMEOUT_S = 60.0
SMOKE_TIMEOUT_S = 120.0
RESTART_TIMEOUT_S = 180.0


def _git(repo_root: str | Path, args: tuple[str, ...]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )


def _with_patch_file(diff: str, run):
    """Run ``run(path)`` with the diff written to a temporary file.

    Measured reason for the file: reading the diff from stdin behaves differently from the documented
    command. ``read_text`` normalizes CRLF to LF while the checked-out file keeps its CRLF, so the
    same patch that applies from a file fails from stdin with "patch failed". The helpers therefore
    use exactly the mechanism the operator-facing command uses — a file path.
    """
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8", newline="") as handle:
        handle.write(diff)
        path = handle.name
    try:
        return run(path)
    finally:
        Path(path).unlink(missing_ok=True)


def patch_state(repo_root: str | Path, diff: str) -> str:
    """``applied`` / ``not_applied`` / ``conflict`` — decided by git, never by guessing.

    A patch that reverses cleanly is already in the tree; one that applies cleanly is not; anything
    else is a conflict the caller must not paper over.
    """
    # 与面板给出的应用命令**同一套参数**（都不带 ``--3way``：导出的补丁没有 ``index`` 行，
    # 三路合并会以 "repository lacks the necessary blob" 失败）。
    def check(args: tuple[str, ...], patch_path: str) -> subprocess.CompletedProcess:
        return _git(repo_root, (*args, patch_path))

    reverse = _with_patch_file(
        diff, lambda path: check(("apply", "-R", "--check", "--whitespace=nowarn"), path)
    )
    if reverse.returncode == 0:
        return "applied"
    forward = _with_patch_file(
        diff, lambda path: check(("apply", "--check", "--whitespace=nowarn"), path)
    )
    return "not_applied" if forward.returncode == 0 else "conflict"


def apply_patch(repo_root: str | Path, diff: str) -> None:
    """Apply the patch for real; a failure raises with git's own exit code, not its output."""
    result = _with_patch_file(
        diff,
        lambda path: _git(repo_root, ("apply", "--whitespace=nowarn", path)),
    )
    if result.returncode != 0:
        raise RuntimeError(f"git apply failed (exit {result.returncode})")


def revert_patch(repo_root: str | Path, diff: str) -> None:
    """Reverse the patch; used when a guard fails after it was applied."""
    result = _with_patch_file(
        diff,
        lambda path: _git(repo_root, ("apply", "-R", "--whitespace=nowarn", path)),
    )
    if result.returncode != 0:
        raise RuntimeError(f"git apply -R failed (exit {result.returncode})")


def patch_paths(diff: str) -> tuple[str, ...]:
    """The paths a diff touches, in order, from its ``+++`` headers."""
    return tuple(
        dict.fromkeys(
            line[6:].strip().removeprefix("b/")
            for line in str(diff or "").splitlines()
            if line.startswith("+++ ") and "/dev/null" not in line
        )
    )


def patch_is_committed(repo_root: str | Path, paths: tuple[str, ...]) -> bool:
    """Are the patch's changes already in a commit rather than sitting in the working tree?

    Only meaningful once the patch is known to be applied: if its content is present and those paths
    are clean relative to HEAD, that content arrived through a commit. Rolling it back with
    ``git apply -R`` would leave the working tree holding the inverse of a commit — a modification
    nobody asked for, and a confusing state to hand anyone. Such a patch is undone with ``git revert``.

    **What it cannot tell.** "Clean" is also what a path nobody ever touched looks like, so on its own
    this says nothing about whether the change is anywhere in the code. Both callers pair it with
    ``patch_state == "applied"`` first, and the close-out path (2026-10-12) additionally verifies the
    content the diff adds is actually present in those files — a probe caught that gap live, after a
    clean-but-untouched file had passed this check alone.
    """
    if not paths:
        return False
    result = _git(repo_root, ("status", "--porcelain", "--untracked-files=no", "--", *paths))
    return result.returncode == 0 and not result.stdout.strip()


def import_smoke(repo_root: str | Path, python: str | None = None) -> tuple[bool, str]:
    """Does the application still import? The cheapest way to catch a patch that cannot start."""
    interpreter = python or sys.executable
    try:
        result = subprocess.run(
            [interpreter, "-c", "import rag.api"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=SMOKE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, "import smoke timed out"
    except OSError as error:
        return False, f"import smoke could not start: {type(error).__name__}"
    if result.returncode == 0:
        return True, ""
    # The tail of stderr is the exception type and message; it is our own code's traceback, not
    # corpus content, and without it a failed patch is undiagnosable.
    tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
    return False, tail[0][:200]


def run_target_tests(
    repo_root: str | Path, test_paths: tuple[str, ...], python: str | None = None
) -> tuple[bool, str]:
    """Run the patch's own verification targets in the live tree (read-only)."""
    if not test_paths:
        return False, "no target tests named"
    interpreter = python or sys.executable
    try:
        result = subprocess.run(
            [interpreter, "-m", "pytest", "-q", *test_paths],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=RESTART_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, "target tests timed out"
    if result.returncode == 0:
        return True, ""
    tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
    return False, tail[0][:200]


def run_full_suite(repo_root: str | Path, python: str | None = None) -> tuple[bool, str]:
    """The full suite, for a caller that wants the verdict synchronously (the helper runs its own)."""
    interpreter = python or sys.executable
    try:
        result = subprocess.run(
            [interpreter, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=2400,
        )
    except subprocess.TimeoutExpired:
        return False, "full suite timed out"
    if result.returncode == 0:
        return True, ""
    tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
    return False, tail[0][:200]


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _healthy(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url}/health", timeout=5) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - any failure is "not healthy yet"
        return False


def log_line(log_path: str | Path, message: str) -> None:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")


#: The detached helper. It is written to disk and started with ``start_new_session``/``DETACHED``
#: so it survives the death of the process it is replacing. Kept as a literal because it must run in
#: a fresh interpreter that has nothing of this one.
HELPER_SOURCE = '''\
"""Restart the service after an approved patch, and undo it if the service does not come back."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

COMMAND = json.loads(sys.argv[1])
# 助手是**同一份仓库**里的进程：把仓库根放进 sys.path，才能复用那套已经测过的租约实现
# （apply/rollback 与重启必须串行在同一个锁上，见 acquire_apply_lease）。
sys.path.insert(0, COMMAND["root"])
from rag.evolution.service_control import acquire_apply_lease  # noqa: E402


def log(message):
    path = Path(COMMAND["log"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\\n")


def port_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((COMMAND["host"], int(COMMAND["port"])))
        except OSError:
            return False
    return True


def healthy():
    try:
        with urllib.request.urlopen(f"{COMMAND['url']}/health", timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


STARTED = {"process": None}


def start():
    STARTED["process"] = subprocess.Popen(  # noqa: S603 - the operator's own launcher
        [COMMAND["python"], str(Path(COMMAND["root"]) / "start.py")],
        cwd=COMMAND["root"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return STARTED["process"]


def stop_service():
    """Stop the service that spawned this helper.

    A restart that cannot stop the running service is not a restart: the port stays busy and the new
    code never comes up. The helper is told which pid to stop, waits a few seconds first so the HTTP
    response that asked for this has time to reach the browser, then terminates it.
    """
    pid = int(COMMAND.get("service_pid") or 0)
    if not pid or pid == os.getpid():
        return
    time.sleep(int(COMMAND.get("grace", 3)))
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as error:
        log(f"could not stop pid {pid}: {type(error).__name__}")
        return
    deadline = time.time() + 30
    while time.time() < deadline and not port_free():
        time.sleep(1)
    log(f"stopped service pid {pid}")


def undo():
    """Undo what this run did — the mirror of its direction.

    应用流程失败时要**反向**补丁；回滚流程失败时要**重新应用**补丁（回到那个已知能跑的状态）。
    两者都是"把系统退回这次动作之前"，只是方向相反。
    """
    reapply = COMMAND.get("direction") == "revert"
    args = ["git", "-C", COMMAND["root"], "apply"]
    if not reapply:
        args.append("-R")
    args.append(COMMAND["patch_file"])
    result = subprocess.run(args, capture_output=True, text=True)
    log(f"undo ({'re-applied' if reapply else 'reverted'}): exit {result.returncode}")


def full_suite_passes():
    """应用后、重启前跑全量套件。补丁自带的 test_paths 是模型自己挑的，拦不住"改坏别处"。"""
    log("running the full test suite before restarting")
    try:
        result = subprocess.run(
            [COMMAND["python"], "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=COMMAND["root"],
            capture_output=True,
            text=True,
            timeout=int(COMMAND["suite_timeout"]),
        )
    except subprocess.TimeoutExpired:
        log("full suite timed out")
        return False
    tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
    log(f"full suite exit {result.returncode}: {tail[0][:120]}")
    return result.returncode == 0


def main():
    log(f"restart helper started for candidate {COMMAND['candidate_id']}")
    # 继承应用请求的租约（C-11）：请求在返回响应之前一直持有它，所以这里要等，
    # 而不是放弃；等不到就**什么都不做** —— 不属于自己的重启比不重启更危险。
    lease = acquire_apply_lease(
        COMMAND["lock"], wait_seconds=float(COMMAND.get("lock_wait") or 60)
    )
    if lease is None:
        log("another apply/rollback still holds the lease; not restarting")
        return
    try:
        run()
    finally:
        lease.release()


def run():
    if COMMAND.get("test_scope") == "full" and not full_suite_passes():
        log("full suite failed; undoing this run's change and NOT restarting")
        undo()
        return
    stop_service()
    deadline = time.time() + 60
    while time.time() < deadline and not port_free():
        time.sleep(1)
    if not port_free():
        log("port never freed; not starting anything")
        return
    start()
    deadline = time.time() + int(COMMAND["timeout"])
    while time.time() < deadline:
        if healthy():
            log("service healthy after restart")
            return
        time.sleep(2)
    log("service did not become healthy; undoing this run's change and restarting")
    # 先结束**自己刚拉起**的那个进程（否则端口被它占着，回滚后的新实例起不来）。
    started = STARTED.get("process")
    if started is not None and started.poll() is None:
        started.terminate()
        try:
            started.wait(timeout=20)
        except Exception:
            pass
    undo()
    deadline = time.time() + 60
    while time.time() < deadline and not port_free():
        time.sleep(1)
    start()
    deadline = time.time() + int(COMMAND["timeout"])
    while time.time() < deadline:
        if healthy():
            log("service healthy after rollback")
            return
        time.sleep(2)
    log("service still not healthy after rollback; a human is needed")


if __name__ == "__main__":
    main()
'''


def spawn_restart_helper(
    *,
    repo_root: str | Path,
    patch_file: str | Path,
    candidate_id: int,
    host: str = "127.0.0.1",
    port: int = 8080,
    url: str = "http://127.0.0.1:8080",
    timeout_s: int = RESTART_TIMEOUT_S,
    test_scope: str = "full",
    suite_timeout_s: int = 2400,
    service_pid: int | None = None,
    grace_s: int = 3,
    direction: str = "apply",
    lock_wait_s: float = HELPER_LOCK_WAIT_S,
) -> Path:
    """Write the helper to disk and start it detached; returns the helper's path.

    The helper **inherits the apply lease** (C-11, 2026-10-22): the command carries the same lock
    file the request held, and the helper takes it before doing anything. A restart is part of the
    same operation, so a second apply must not start while this one's helper is still swapping the
    process — and the request that spawned it only releases the lease when it returns, which is why
    the helper waits (bounded by ``lock_wait_s``) instead of failing fast.
    """
    root = Path(repo_root)
    helper = root / "artifacts" / f"restart-candidate-{int(candidate_id)}.py"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text(HELPER_SOURCE, encoding="utf-8")
    command = {
        "root": str(root),
        "python": sys.executable,
        "patch_file": str(Path(patch_file)),
        "candidate_id": int(candidate_id),
        "host": host,
        "port": int(port),
        "url": url,
        "timeout": int(timeout_s),
        "test_scope": str(test_scope),
        "suite_timeout": int(suite_timeout_s),
        "service_pid": int(service_pid if service_pid is not None else os.getpid()),
        # 这份助手是替哪一次动作善后的：apply ⇒ 失败时反向；revert ⇒ 失败时重新应用。
        "direction": "revert" if str(direction) == "revert" else "apply",
        "grace": int(grace_s),
        "log": str(service_log_path(root)),
        # 继承应用请求的租约（C-11）：同一个文件，助手开工前先取到它。
        "lock": str(apply_lock_path(root)),
        "lock_wait": float(lock_wait_s),
    }
    flags = 0
    if os.name == "nt":  # pragma: no cover - platform specific
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    subprocess.Popen(  # noqa: S603 - our own helper, with an absolute interpreter
        [sys.executable, str(helper), json.dumps(command)],
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
        creationflags=flags,
    )
    log_line(service_log_path(root), f"restart requested for candidate {int(candidate_id)}")
    return helper



__all__ = [
    "HELPER_LOCK_WAIT_S",
    "LOCK_NAME",
    "LOG_NAME",
    "LOG_DIR_ENV",
    "acquire_apply_lease",
    "apply_lock_path",
    "apply_patch",
    "import_smoke",
    "log_line",
    "patch_state",
    "revert_patch",
    "run_full_suite",
    "run_target_tests",
    "service_log_path",
    "spawn_restart_helper",
]
