"""应用/回滚/状态判断：全部在**临时 git 仓库**上验证，绝不动真实工作树。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from rag.evolution.service_control import (
    HELPER_SOURCE,
    apply_patch,
    import_smoke,
    patch_state,
    revert_patch,
    run_full_suite,
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

def test_the_helper_source_compiles(tmp_path):
    """助手是写进磁盘再运行的**字符串**：语法错只会在运行时炸，所以这里显式编译一次。"""
    compile(HELPER_SOURCE, "restart-helper", "exec")
    assert "stop_service" in HELPER_SOURCE  # 不会停服务就不是"重启"
    assert "full_suite_passes" in HELPER_SOURCE
