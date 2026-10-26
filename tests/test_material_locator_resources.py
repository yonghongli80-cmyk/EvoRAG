from __future__ import annotations

from pathlib import Path

import pytest

from scripts.material_locator_resources import (
    ResourceGateError,
    enforce_resource_gate,
    sample_resources,
)


def _sample_linux(proc: Path, sysroot: Path):
    return sample_resources(platform_name="linux", proc_root=proc, sys_root=sysroot, page_size_bytes=4096)


def _linux_v2_tree(tmp_path: Path, *, leaf_limit: str = "900", parent_limit: str = "500"):
    proc = tmp_path / "proc"
    sysroot = tmp_path / "sys"
    (proc / "self").mkdir(parents=True)
    mount = sysroot / "fs" / "cgroup"
    (mount / "team" / "worker").mkdir(parents=True)
    (mount / "team").mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text("MemTotal: 4096000 kB\nMemAvailable: 1048576 kB\n")
    (proc / "self" / "statm").write_text("1000 100\n")
    (proc / "self" / "cgroup").write_text("0::/team/worker\n")
    (proc / "self" / "mountinfo").write_text(
        "29 23 0:26 / /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw\n"
    )
    (mount / "memory.max").write_text("1000\n")
    (mount / "memory.current").write_text("800\n")
    (mount / "team" / "memory.max").write_text(parent_limit + "\n")
    (mount / "team" / "memory.current").write_text("450\n")
    (mount / "team" / "worker" / "memory.max").write_text(leaf_limit + "\n")
    (mount / "team" / "worker" / "memory.current").write_text("300\n")
    return proc, sysroot


def _linux_v1_tree(tmp_path: Path, *, limit: str = "700"):
    proc = tmp_path / "proc"
    sysroot = tmp_path / "sys"
    (proc / "self").mkdir(parents=True)
    mount = sysroot / "fs" / "cgroup" / "memory"
    (mount / "dept" / "worker").mkdir(parents=True)
    (mount / "dept").mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text("MemTotal: 4096000 kB\nMemAvailable: 1024000 kB\n")
    (proc / "self" / "statm").write_text("1000 100\n")
    (proc / "self" / "cgroup").write_text("5:cpu,memory:/dept/worker\n")
    (proc / "self" / "mountinfo").write_text(
        "30 23 0:27 / /sys/fs/cgroup/memory rw,nosuid,nodev,noexec,relatime - cgroup cgroup rw,memory\n"
    )
    (mount / "memory.limit_in_bytes").write_text("9223372036854771712\n")
    (mount / "memory.usage_in_bytes").write_text("250\n")
    (mount / "dept" / "memory.limit_in_bytes").write_text("500\n")
    (mount / "dept" / "memory.usage_in_bytes").write_text("400\n")
    (mount / "dept" / "worker" / "memory.limit_in_bytes").write_text(limit + "\n")
    (mount / "dept" / "worker" / "memory.usage_in_bytes").write_text("300\n")
    return proc, sysroot


def test_linux_v2_uses_minimum_host_leaf_and_visible_ancestor_headroom(tmp_path):
    proc, sysroot = _linux_v2_tree(tmp_path)

    sample = _sample_linux(proc, sysroot)

    assert sample["host_available_bytes"] == 1024 * 1024 * 1024
    assert sample["process_rss_bytes"] > 0
    assert sample["cgroup_version"] == 2
    assert sample["cgroup_visible_ancestor_count"] == 3
    assert sample["effective_available_bytes"] == 50
    assert sample["cgroup_visibility"]


def test_linux_v2_max_is_unlimited_but_parent_limit_still_applies(tmp_path):
    proc, sysroot = _linux_v2_tree(tmp_path, leaf_limit="max", parent_limit="max")
    (sysroot / "fs" / "cgroup" / "memory.max").write_text("max\n")
    (sysroot / "fs" / "cgroup" / "memory.current").write_text("800\n")
    sample = _sample_linux(proc, sysroot)
    assert sample["effective_available_bytes"] == sample["host_available_bytes"]
    assert sample["cgroup_unlimited"] is True


def test_linux_v1_sentinel_is_unlimited_and_visible_parent_limits_headroom(tmp_path):
    proc, sysroot = _linux_v1_tree(tmp_path)

    sample = _sample_linux(proc, sysroot)

    assert sample["cgroup_version"] == 1
    assert sample["cgroup_visible_ancestors"][0]["limit_bytes"] is None
    assert sample["cgroup_unlimited"] is False
    assert sample["effective_available_bytes"] == 100
    assert sample["cgroup_visible_ancestor_count"] == 3


def test_linux_v1_finite_leaf_limit_contributes_to_headroom(tmp_path):
    proc, sysroot = _linux_v1_tree(tmp_path, limit="700")
    sample = _sample_linux(proc, sysroot)
    assert sample["effective_available_bytes"] == 100


def test_linux_v2_clamps_over_limit_usage_to_zero_headroom(tmp_path):
    proc, sysroot = _linux_v2_tree(tmp_path)
    (sysroot / "fs" / "cgroup" / "team" / "memory.current").write_text("501\n")
    sample = _sample_linux(proc, sysroot)
    assert sample["effective_available_bytes"] == 0


def test_namespace_hidden_cgroup_ancestor_is_reported_as_a_boundary(tmp_path):
    proc, sysroot = _linux_v2_tree(tmp_path)
    mount = sysroot / "fs" / "cgroup"
    (proc / "self" / "cgroup").write_text("0::/\n")
    (proc / "self" / "mountinfo").write_text(
        "29 23 0:26 /tenant /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw\n"
    )
    (mount / "memory.max").write_text("500\n")
    (mount / "memory.current").write_text("400\n")
    sample = _sample_linux(proc, sysroot)
    assert sample["cgroup_namespace_root_visible"] is True
    assert "boundary" in sample["cgroup_visibility"]
    assert "namespace-hidden" in sample["cgroup_boundary_note"]


@pytest.mark.parametrize("failure", ["missing_limit", "malformed_limit", "missing_current", "path_escape"])
def test_unmeasurable_linux_cgroup_fails_closed(tmp_path, failure):
    proc, sysroot = _linux_v2_tree(tmp_path)
    mount = sysroot / "fs" / "cgroup"
    if failure == "missing_limit":
        (mount / "team" / "worker" / "memory.max").unlink()
    elif failure == "malformed_limit":
        (mount / "team" / "worker" / "memory.max").write_text("unknown\n")
    elif failure == "missing_current":
        (mount / "team" / "worker" / "memory.current").unlink()
    else:
        (proc / "self" / "cgroup").write_text("0::/../../outside\n")

    with pytest.raises(ResourceGateError):
        _sample_linux(proc, sysroot)


def test_unmeasurable_gate_sample_is_rejected():
    with pytest.raises(ResourceGateError):
        enforce_resource_gate(
            {"sampled": False, "error": "cgroup_unreadable"},
            min_free_bytes=1,
            max_working_set_bytes=10,
        )


def test_resource_gate_checks_effective_free_and_rss():
    with pytest.raises(ResourceGateError):
        enforce_resource_gate(
            {
                "sampled": True,
                "effective_available_bytes": 9,
                "process_rss_bytes": 11,
                "below_normal_priority_set": True,
                "blas_threads": "1",
                "thread_limits": {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"},
            },
            min_free_bytes=10,
            max_working_set_bytes=10,
        )
