"""应用/回滚/状态判断：全部在**临时 git 仓库**上验证，绝不动真实工作树。"""

from __future__ import annotations

import json
import subprocess
import time
import types
from pathlib import Path

from rag.evolution import service_control as control
from rag.evolution.service_control import (
    HELPER_SOURCE,
    acquire_apply_lease,
    apply_lock_path,
    apply_patch,
    import_smoke,
    patch_state,
    revert_patch,
    run_full_suite,
    spawn_restart_helper,
)


def _temp_repo(tmp_path: Path) -> tuple[Path, str]:
    """一个小仓库 + 一份能应用的补丁（把 alpha 换成 beta）。"""
    repo = tmp_path / "repo"
    (repo / "rag").mkdir(parents=True)
    target = repo / "rag" / "demo.py"
    target.write_text("alpha = 1\ngamma = 3\n", encoding="utf-8")
    for args in (("init", "-q"), ("add", "-A"), ("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    diff = (
        "--- a/rag/demo.py\n"
        "+++ b/rag/demo.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-alpha = 1\n"
        "+beta = 1\n"
        " gamma = 3\n"
    )
    return repo, diff


def test_state_apply_and_revert_round_trip(tmp_path):
    repo, diff = _temp_repo(tmp_path)

    assert patch_state(repo, diff) == "not_applied"

    apply_patch(repo, diff)
    assert patch_state(repo, diff) == "applied"
    assert "beta = 1" in (repo / "rag" / "demo.py").read_text(encoding="utf-8")

    revert_patch(repo, diff)
    assert patch_state(repo, diff) == "not_applied"
    assert "alpha = 1" in (repo / "rag" / "demo.py").read_text(encoding="utf-8")


def test_a_patch_that_no_longer_matches_is_a_conflict(tmp_path):
    repo, diff = _temp_repo(tmp_path)
    (repo / "rag" / "demo.py").write_text("completely different\n", encoding="utf-8")

    assert patch_state(repo, diff) == "conflict"


def test_import_smoke_reports_a_missing_interpreter(tmp_path):
    ok, why = import_smoke(tmp_path, python=str(tmp_path / "no-such-python"))

    assert ok is False
    assert "could not start" in why

def test_full_suite_gate_passes_and_fails_on_a_tiny_repo(tmp_path):
    """应用前的全量测试闸：补丁自带的 test_paths 是模型自己挑的，拦不住改坏别处。"""
    repo = tmp_path / "suite"
    repo.mkdir()
    test_file = repo / "test_ok.py"
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    ok, why = run_full_suite(repo)
    assert ok is True and why == ""

    test_file.write_text("def test_ok():\n    assert False\n", encoding="utf-8")
    ok, why = run_full_suite(repo)
    assert ok is False
    assert "failed" in why.lower() or "assert" in why.lower()

def _real_service_log_path():
    """Overwrite the fixture's stand-in with the real function, so the default can be asserted.

    ``conftest.isolated_service_control_log`` replaces the module attribute for every test (that is
    what keeps the suite out of the operator's log). Reading the original out of the source file is
    the only way to test the function itself from inside the suite.
    """
    import importlib.util
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "rag" / "evolution" / "service_control.py"
    spec = importlib.util.spec_from_file_location("_service_control_source", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.service_log_path


def test_service_log_path_defaults_to_artifacts_and_follows_the_override(tmp_path, monkeypatch):
    """日志目的地只有一个决定点：默认落 artifacts，``RAG_EVOLUTION_SERVICE_LOG_DIR`` 可改道。"""
    from rag.evolution.service_control import LOG_DIR_ENV, LOG_NAME

    service_log_path = _real_service_log_path()

    monkeypatch.delenv(LOG_DIR_ENV, raising=False)
    assert service_log_path(tmp_path) == tmp_path / "artifacts" / LOG_NAME

    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "elsewhere"))
    assert service_log_path(tmp_path) == tmp_path / "elsewhere" / LOG_NAME


def test_the_helper_source_compiles(tmp_path):
    """助手是写进磁盘再运行的**字符串**：语法错只会在运行时炸，所以这里显式编译一次。"""
    compile(HELPER_SOURCE, "restart-helper", "exec")
    assert "stop_service" in HELPER_SOURCE  # 不会停服务就不是"重启"
    assert "full_suite_passes" in HELPER_SOURCE


# ------------------------------------------------------------------
# C-11 (2026-10-22): the apply lease, and the helper inheriting it.
# ------------------------------------------------------------------


def test_acquire_apply_lease_waits_for_the_holder_then_gives_up(tmp_path):
    """助手必须**等**请求释放租约（请求在响应返回前一直持有），等不到就什么都不做。"""
    from rag.evolution.host_lock import host_measurement_lock

    lock_path = tmp_path / "apply.lock"

    with host_measurement_lock(lock_path):
        started = time.monotonic()
        lease = acquire_apply_lease(lock_path, wait_seconds=0.2, poll_seconds=0.05)
        elapsed = time.monotonic() - started
        assert lease is None
        assert elapsed >= 0.15, "它应当等满窗口，而不是立刻放弃"

    lease = acquire_apply_lease(lock_path, wait_seconds=0.5, poll_seconds=0.05)
    assert lease is not None
    try:
        # 已持有时再取一次＝重入：拒绝（与 API 那边同一个语义），不嵌套。
        assert acquire_apply_lease(lock_path, wait_seconds=0.0) is None
    finally:
        lease.release()

    assert not lock_path.exists()
    reacquired = acquire_apply_lease(lock_path, wait_seconds=0.0)
    assert reacquired is not None
    reacquired.release()


def test_the_helper_source_inherits_the_apply_lease():
    """租约是**继承**的：助手在自己的进程里取同一个锁文件，而不是另起一套。"""
    compile(HELPER_SOURCE, "restart-helper", "exec")
    assert "acquire_apply_lease" in HELPER_SOURCE
    assert 'COMMAND["lock"]' in HELPER_SOURCE
    assert "not restarting" in HELPER_SOURCE  # 等不到就不重启


def test_spawn_restart_helper_hands_the_lease_to_the_helper(tmp_path, monkeypatch):
    """命令里必须带锁文件与等待窗口，助手才知道该继承哪一个租约。"""
    captured: dict = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = list(argv)
            captured["command"] = json.loads(argv[-1])

    monkeypatch.setattr(
        control,
        "subprocess",
        types.SimpleNamespace(
            Popen=FakePopen,
            DEVNULL=subprocess.DEVNULL,
            DETACHED_PROCESS=0,
            CREATE_NEW_PROCESS_GROUP=0,
        ),
    )

    helper = spawn_restart_helper(
        repo_root=tmp_path, patch_file=tmp_path / "candidate.patch", candidate_id=7
    )

    command = captured["command"]
    assert helper.is_file()
    assert command["lock"] == str(apply_lock_path(tmp_path))
    assert command["lock_wait"] > 0
    assert command["candidate_id"] == 7
