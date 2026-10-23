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

Operational diagnostics are logged to ``artifacts/service-control.log``. Candidate status changes
are also recorded through the candidate store's checked lifecycle transition and audit transaction;
the separate diagnosis audit retains its constrained action vocabulary.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
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
#: the same OS-backed lease abstraction as the host A/B lock (:mod:`rag.evolution.host_lock`).
LOCK_NAME = "evolution-apply.lock"
OPERATION_MARKER_NAME = "evolution-apply.pending.json"

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


def operation_marker_path(repo_root: str | Path) -> Path:
    """Reservation that closes the API-release/helper-acquire lease handoff gap."""
    return service_log_path(repo_root).parent / OPERATION_MARKER_NAME


def create_operation_artifact(
    repo_root: str | Path,
    diff: str,
    *,
    candidate_id: int,
    database_path: str | Path,
    direction: str,
    expected_status: str,
    compensation_status: str,
    actor_id: str,
    applied_now: bool,
) -> dict:
    """Persist an operation-specific patch snapshot and the metadata its helper must verify."""
    operation_id = uuid.uuid4().hex
    directory = service_log_path(repo_root).parent / "evolution-operations"
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = directory / f"candidate-{int(candidate_id)}-{operation_id}.patch"
    content = str(diff).encode("utf-8")
    with snapshot.open("xb") as handle:
        handle.write(content)
    with contextlib.suppress(OSError):
        snapshot.chmod(0o444)
    return {
        "operation_id": operation_id,
        "candidate_id": int(candidate_id),
        "patch_file": str(snapshot),
        "patch_sha256": hashlib.sha256(content).hexdigest(),
        "database_path": str(Path(database_path).resolve()),
        "direction": "revert" if direction == "revert" else "apply",
        "expected_status": str(expected_status),
        "compensation_status": str(compensation_status),
        "actor_id": str(actor_id),
        "applied_now": bool(applied_now),
        "marker_path": str(operation_marker_path(repo_root)),
    }


def verify_operation_artifact(operation: dict) -> bool:
    """Verify that the helper's exact patch snapshot is still the one authorized."""
    try:
        content = Path(operation["patch_file"]).read_bytes()
    except (OSError, KeyError, TypeError):
        return False
    return hashlib.sha256(content).hexdigest() == operation.get("patch_sha256")


def arm_operation_marker(operation: dict) -> None:
    """Reserve the lease handoff; an existing marker requires operator recovery."""
    path = Path(operation["marker_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(operation, handle, ensure_ascii=False, sort_keys=True)


def clear_operation_marker(marker_path: str | Path, operation_id: str) -> bool:
    """Remove only this operation's reservation, never another operation's marker."""
    path = Path(marker_path)
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if marker.get("operation_id") != operation_id:
        return False
    path.unlink()
    return True


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
import hashlib
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
        return True
    time.sleep(int(COMMAND.get("grace", 3)))
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as error:
        log(f"could not stop pid {pid}: {type(error).__name__}")
        return False
    deadline = time.time() + 30
    while time.time() < deadline and not port_free():
        time.sleep(1)
    stopped = port_free()
    log(f"stopped service pid {pid}" if stopped else f"service pid {pid} did not release the port")
    return stopped


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
    return result.returncode == 0


def advance_candidate_status(status, note):
    """Use the candidate store's checked transition and audit transaction."""
    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.store import FailureTraceStore

    trace_store = FailureTraceStore(COMMAND["database_path"])
    try:
        candidates = CandidateStore(trace_store)
        candidate = candidates.get_candidate(int(COMMAND["candidate_id"]))
        expected_current = str(COMMAND["expected_status"])
        if candidate is None or candidate["status"] != expected_current:
            got = None if candidate is None else candidate["status"]
            log(f"candidate status mismatch: expected {expected_current}, got {got}")
            return False
        candidates.advance_candidate(
            int(COMMAND["candidate_id"]), str(status),
            actor_id=str(COMMAND["actor_id"]), note=note,
        )
        return True
    except Exception as error:
        log(f"candidate lifecycle update failed: {type(error).__name__}: {error}")
        return False
    finally:
        trace_store.close()


def record_compensation():
    if COMMAND.get("direction") != "apply" or not COMMAND.get("applied_now"):
        return True
    return advance_candidate_status(
        COMMAND["compensation_status"],
        f"helper补偿：应用失败后已反向应用补丁（operation={COMMAND['operation_id']}）",
    )


def record_success():
    status = COMMAND.get("success_status")
    if not status:
        return True
    return advance_candidate_status(
        status,
        f"helper完成：补丁操作及服务健康检查成功（operation={COMMAND['operation_id']}）",
    )


def compensate_and_report(reason):
    if COMMAND.get("direction") != "revert" and not COMMAND.get("applied_now"):
        log(f"{reason}; patch predates this operation, leaving it untouched")
        return True
    log(f"{reason}; restoring the pre-operation tree")
    if not undo():
        log("compensation failed; operation marker retained and no restart will follow")
        return False
    if not record_compensation():
        log("tree restored but candidate state could not be reconciled; marker retained")
        return False
    return True


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
        marker_path = Path(COMMAND["marker_path"])
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            digest = hashlib.sha256(Path(COMMAND["patch_file"]).read_bytes()).hexdigest()
        except (OSError, json.JSONDecodeError) as error:
            log(f"operation artifact unavailable: {type(error).__name__}; marker retained")
            return
        if marker.get("operation_id") != COMMAND.get("operation_id"):
            log("operation marker belongs to another operation; no action taken")
            return
        if digest != COMMAND.get("patch_sha256") or digest != marker.get("patch_sha256"):
            log("operation patch snapshot hash mismatch; marker retained for manual recovery")
            return
        if run():
            try:
                marker_path.unlink()
                log("operation completed; lease handoff marker cleared")
            except OSError as error:
                log(f"operation completed but marker could not be cleared: {type(error).__name__}")
        else:
            log("operation outcome uncertain; marker retained; inspect operation artifact before retry")
    finally:
        lease.release()


def run():
    if COMMAND.get("test_scope") == "full" and not full_suite_passes():
        log("full suite failed; undoing this run's change and NOT restarting")
        return compensate_and_report("full suite failed")
    if not stop_service():
        log("service could not be stopped; no restart will follow")
        return False
    deadline = time.time() + 60
    while time.time() < deadline and not port_free():
        time.sleep(1)
    if not port_free():
        if not compensate_and_report("port never freed"):
            return False
        return restart_previous_service()
    try:
        start()
    except Exception as error:
        if not compensate_and_report(f"service could not start: {type(error).__name__}"):
            return False
        return restart_previous_service()
    deadline = time.time() + int(COMMAND["timeout"])
    while time.time() < deadline:
        if healthy():
            log("service healthy after restart")
            if record_success():
                return True
            log("service is healthy but candidate status was not finalized; marker retained")
            return False
        time.sleep(2)
    log("service did not become healthy; undoing this run's change and restarting")
    # 先结束**自己刚拉起**的那个进程（否则端口被它占着，回滚后的新实例起不来）。
    started = STARTED.get("process")
    if started is not None and started.poll() is None:
        try:
            started.terminate()
            started.wait(timeout=20)
        except Exception as error:
            log(f"new service process could not be stopped: {type(error).__name__}")
            return False
    if not compensate_and_report("service health check failed"):
        return False
    return restart_previous_service()


def restart_previous_service():
    """Restore the pre-operation code and make a best effort to bring it back online."""
    deadline = time.time() + 60
    while time.time() < deadline and not port_free():
        time.sleep(1)
    try:
        start()
    except Exception as error:
        log(f"previous service could not restart: {type(error).__name__}")
        return False
    deadline = time.time() + int(COMMAND["timeout"])
    while time.time() < deadline:
        if healthy():
            log("service healthy after rollback")
            return True
        time.sleep(2)
    log("service still not healthy after rollback; a human is needed")
    return False


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
    operation: dict | None = None,
) -> Path:
    """Write the helper to disk and start it detached; returns the helper's path.

    The helper **inherits the apply lease** (C-11, 2026-10-22): the command carries the same lock
    file the request held, and the helper takes it before doing anything. A restart is part of the
    same operation, so a second apply must not start while this one's helper is still swapping the
    process — and the request that spawned it only releases the lease when it returns, which is why
    the helper waits (bounded by ``lock_wait_s``) instead of failing fast.
    """
    root = Path(repo_root)
    operation = dict(operation or {})
    operation_id = str(operation.get("operation_id") or uuid.uuid4().hex)
    helper = service_log_path(root).parent / f"restart-candidate-{int(candidate_id)}-{operation_id}.py"
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
        "operation_id": operation_id,
        "patch_sha256": str(operation.get("patch_sha256") or ""),
        "database_path": str(operation.get("database_path") or ""),
        "expected_status": str(operation.get("expected_status") or ""),
        "compensation_status": str(operation.get("compensation_status") or ""),
        "success_status": str(operation.get("success_status") or ""),
        "actor_id": str(operation.get("actor_id") or "system"),
        "applied_now": bool(operation.get("applied_now", False)),
        "marker_path": str(operation.get("marker_path") or ""),
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
    "OPERATION_MARKER_NAME",
    "LOG_NAME",
    "LOG_DIR_ENV",
    "acquire_apply_lease",
    "apply_lock_path",
    "arm_operation_marker",
    "apply_patch",
    "import_smoke",
    "log_line",
    "patch_state",
    "operation_marker_path",
    "revert_patch",
    "run_full_suite",
    "run_target_tests",
    "service_log_path",
    "spawn_restart_helper",
    "clear_operation_marker",
    "create_operation_artifact",
    "verify_operation_artifact",
]
