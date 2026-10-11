"""宿主 A/B 的独占锁：并发争用会被记成候选的问题，所以宁可拒绝启动第二次。

实测动机（2026-10-11）：两次并发对照（或对照与全量测试重叠）让基线侧出现 **26 次调用失败**，
而单独跑同一条命令两次都是 0。失败调用看起来像候选的缺陷 —— 与环境类拒跑是同一类错误。
"""

from __future__ import annotations

import json
import os
import time

import pytest

from rag.evolution.host_lock import HostLocked, host_measurement_lock


def test_a_second_measurement_is_refused_while_one_holds_the_host(tmp_path):
    lock_path = tmp_path / "host.lock"

    holder = host_measurement_lock(lock_path)
    holder.__enter__()
    try:
        with pytest.raises(HostLocked) as busy:
            host_measurement_lock(lock_path).__enter__()
        assert "already running" in str(busy.value)
        assert str(os.getpid()) in str(busy.value)
    finally:
        holder.__exit__(None, None, None)

    # 释放之后可以再取
    with host_measurement_lock(lock_path):
        pass


def _raise_inside_the_lock(lock_path) -> None:
    with host_measurement_lock(lock_path):
        raise RuntimeError("测量炸了")


def test_the_lock_is_released_even_when_the_measurement_raises(tmp_path):
    lock_path = tmp_path / "host.lock"

    with pytest.raises(RuntimeError):
        _raise_inside_the_lock(lock_path)

    assert not lock_path.exists(), "释放必须发生在异常路径上"
    with host_measurement_lock(lock_path):
        pass


def test_a_lock_left_by_a_dead_process_is_taken_over(tmp_path):
    """被 kill 的运行不该永久挡住后面的测量：pid 已不在 ⇒ 接管，并把接管事实报出来。"""
    lock_path = tmp_path / "host.lock"
    lock_path.write_text(json.dumps({"pid": 999_999_999, "taken_at": time.time()}), encoding="utf-8")

    with host_measurement_lock(lock_path) as takeover:
        assert takeover["pid"] == 999_999_999

    assert not lock_path.exists()


def test_an_ancient_lock_is_taken_over_even_if_the_pid_lives(tmp_path):
    import rag.evolution.host_lock as module

    lock_path = tmp_path / "host.lock"
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "taken_at": time.time() - module.STALE_AFTER_S - 1}),
        encoding="utf-8",
    )

    with host_measurement_lock(lock_path) as takeover:
        assert takeover["pid"] == os.getpid()
