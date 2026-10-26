"""Read-only resource sampling and fail-closed gates for locator acceptance."""

from __future__ import annotations

import os
import platform
import re
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any


class ResourceGateError(RuntimeError):
    """A required resource measurement failed or crossed its configured limit."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {"error_code": "resource_gate_failed", "message": message}


_V1_UNLIMITED = (1 << 60)
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def _decode_mount_path(value: str) -> str:
    return _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _read_int(path: Path, *, label: str) -> int:
    try:
        value = path.read_text(encoding="ascii").strip()
        number = int(value)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ResourceGateError(
            f"cannot read {label}",
            {"error_code": "resource_measurement_unavailable", "field": label, "path": str(path)},
        ) from exc
    if number < 0:
        raise ResourceGateError(
            f"negative {label}",
            {"error_code": "resource_measurement_invalid", "field": label, "path": str(path)},
        )
    return number


def _proc_memory(proc_root: Path, *, page_size_bytes: int | None = None) -> tuple[int, int]:
    meminfo = proc_root / "meminfo"
    try:
        fields = {}
        for line in meminfo.read_text(encoding="ascii").splitlines():
            key, separator, rest = line.partition(":")
            if separator:
                fields[key] = rest.strip().split()
        available_value = int(fields["MemAvailable"][0])
        available_unit = fields["MemAvailable"][1].lower()
    except (OSError, UnicodeError, KeyError, IndexError, ValueError) as exc:
        raise ResourceGateError(
            "cannot read Linux MemAvailable",
            {"error_code": "resource_measurement_unavailable", "field": "MemAvailable", "path": str(meminfo)},
        ) from exc
    if available_value < 0 or available_unit not in {"kb", "kib"}:
        raise ResourceGateError(
            "unexpected MemAvailable unit",
            {"error_code": "resource_measurement_invalid", "field": "MemAvailable", "unit": available_unit},
        )
    available = available_value * 1024
    try:
        statm = (proc_root / "self" / "statm").read_text(encoding="ascii").split()
        resident_pages = int(statm[1])
        page_size = page_size_bytes if page_size_bytes is not None else int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, UnicodeError, IndexError, ValueError, AttributeError) as exc:
        raise ResourceGateError(
            "cannot read Linux process RSS",
            {"error_code": "resource_measurement_unavailable", "field": "process_rss", "path": str(proc_root / "self" / "statm")},
        ) from exc
    if resident_pages < 0 or page_size <= 0:
        raise ResourceGateError(
            "invalid Linux process RSS",
            {"error_code": "resource_measurement_invalid", "field": "process_rss"},
        )
    return available, resident_pages * page_size


def _mount_records(proc_root: Path) -> list[dict[str, str]]:
    path = proc_root / "self" / "mountinfo"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ResourceGateError(
            "cannot read cgroup mount table",
            {"error_code": "cgroup_mountinfo_unavailable", "path": str(path)},
        ) from exc
    records: list[dict[str, str]] = []
    for line in lines:
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 6 or len(right_fields) < 3:
            continue
        fstype = right_fields[0]
        if fstype not in {"cgroup", "cgroup2"}:
            continue
        records.append({
            "root": _decode_mount_path(left_fields[3]),
            "mountpoint": _decode_mount_path(left_fields[4]),
            "mount_options": left_fields[5],
            "fstype": fstype,
            "source": right_fields[1],
            "super_options": right_fields[2],
        })
    return records


def _proc_cgroups(proc_root: Path) -> list[tuple[str, str, str]]:
    path = proc_root / "self" / "cgroup"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ResourceGateError(
            "cannot read process cgroup membership",
            {"error_code": "cgroup_membership_unavailable", "path": str(path)},
        ) from exc
    memberships: list[tuple[str, str, str]] = []
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        memberships.append((fields[0], fields[1], _decode_mount_path(fields[2])))
    if not memberships:
        raise ResourceGateError(
            "process cgroup membership is empty or malformed",
            {"error_code": "cgroup_membership_invalid", "path": str(path)},
        )
    return memberships


def _mounted_root(sys_root: Path, mountpoint: str) -> Path:
    posix = PurePosixPath(mountpoint)
    if not posix.is_absolute() or ".." in posix.parts:
        raise ResourceGateError(
            "invalid cgroup mountpoint",
            {"error_code": "cgroup_mount_path_invalid", "mountpoint": mountpoint},
        )
    # Injectable roots make Linux proc fixtures testable on Windows while the
    # default /sys root maps to the kernel's literal mountpoint.
    parts = posix.parts[1:]
    if sys_root.resolve() == Path("/sys").resolve():
        path = Path(mountpoint)
    elif parts and parts[0] == "sys":
        path = sys_root.joinpath(*parts[1:])
    else:
        raise ResourceGateError(
            "cannot map a non-sysfs cgroup mount into the injected sysfs root",
            {"error_code": "cgroup_mount_mapping_unavailable", "mountpoint": mountpoint},
        )
    try:
        path.resolve().relative_to(sys_root.resolve())
    except (OSError, ValueError) as exc:
        raise ResourceGateError(
            "cgroup mountpoint escapes the supplied sysfs root",
            {"error_code": "cgroup_mount_path_escape", "mountpoint": mountpoint},
        ) from exc
    return path


def _path_is_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def _cgroup_directory(mount_root: str, mountpoint: str, membership: str, sys_root: Path) -> tuple[Path, Path, bool]:
    root_posix = PurePosixPath(mount_root)
    member_posix = PurePosixPath(membership)
    if not root_posix.is_absolute() or not member_posix.is_absolute() or ".." in root_posix.parts or ".." in member_posix.parts:
        raise ResourceGateError(
            "invalid cgroup root or membership path",
            {"error_code": "cgroup_path_invalid", "root": mount_root, "membership": membership},
        )
    mount_dir = _mounted_root(sys_root, mountpoint)
    visible_boundary = not _path_is_within(member_posix, root_posix)
    if visible_boundary:
        # In a cgroup namespace the membership path can be rooted below the
        # host mount root. Treat the mountpoint as the visible namespace root
        # only when the kernel reports the namespace root as '/'.
        if member_posix != PurePosixPath("/"):
            raise ResourceGateError(
                "cgroup membership does not map inside the mounted hierarchy",
                {"error_code": "cgroup_path_mount_mismatch", "root": mount_root, "membership": membership},
            )
        relative = PurePosixPath()
        visible_boundary = True
    else:
        relative = member_posix.relative_to(root_posix)
    leaf = mount_dir.joinpath(*relative.parts)
    try:
        leaf.resolve().relative_to(mount_dir.resolve())
    except (OSError, ValueError) as exc:
        raise ResourceGateError(
            "cgroup membership escapes the mounted hierarchy",
            {"error_code": "cgroup_path_escape", "membership": membership},
        ) from exc
    return mount_dir, leaf, visible_boundary


def resolve_cgroup_memory(proc_root: Path, sys_root: Path) -> dict[str, Any]:
    """Read the process cgroup memory limit and every visible ancestor limit."""
    memberships = _proc_cgroups(proc_root)
    mounts = _mount_records(proc_root)
    v2_members = [entry for entry in memberships if entry[0] == "0" and not entry[1]]
    if v2_members:
        hierarchy_id, _controllers, membership = v2_members[0]
        candidates = [mount for mount in mounts if mount["fstype"] == "cgroup2"]
        version = 2
        limit_name, current_name = "memory.max", "memory.current"
        controller = "memory"
    else:
        memory_member = next((entry for entry in memberships if "memory" in entry[1].split(",")), None)
        if memory_member is None:
            raise ResourceGateError(
                "memory cgroup membership is not visible",
                {"error_code": "cgroup_memory_membership_missing"},
            )
        hierarchy_id, _controllers, membership = memory_member
        candidates = [
            mount for mount in mounts
            if mount["fstype"] == "cgroup"
            and "memory" in (mount["mount_options"] + "," + mount["super_options"]).split(",")
        ]
        version = 1
        limit_name, current_name = "memory.limit_in_bytes", "memory.usage_in_bytes"
        controller = "memory"
    if not candidates:
        raise ResourceGateError(
            "memory cgroup mount is not visible",
            {"error_code": "cgroup_memory_mount_missing", "version": version},
        )

    failures: list[dict[str, Any]] = []
    for mount in candidates:
        try:
            mount_dir, leaf, namespace_rooted = _cgroup_directory(
                mount["root"], mount["mountpoint"], membership, sys_root
            )
            chain: list[Path] = []
            current = leaf
            while True:
                chain.append(current)
                if current == mount_dir:
                    break
                current = current.parent
            chain.reverse()
            ancestors: list[dict[str, Any]] = []
            for directory in chain:
                limit_path = directory / limit_name
                current_path = directory / current_name
                if version == 2:
                    try:
                        token = limit_path.read_text(encoding="ascii").strip()
                    except (OSError, UnicodeError) as exc:
                        raise ResourceGateError(
                            "cannot read cgroup v2 memory.max",
                            {"error_code": "cgroup_limit_unavailable", "path": str(limit_path)},
                        ) from exc
                    if token == "max":
                        limit = None
                    else:
                        try:
                            limit = int(token)
                        except ValueError as exc:
                            raise ResourceGateError(
                                "invalid cgroup v2 memory.max",
                                {"error_code": "cgroup_limit_invalid", "path": str(limit_path)},
                            ) from exc
                        if limit < 0:
                            raise ResourceGateError(
                                "negative cgroup v2 memory.max",
                                {"error_code": "cgroup_limit_invalid", "path": str(limit_path)},
                            )
                else:
                    raw_limit = _read_int(limit_path, label="cgroup v1 memory limit")
                    limit = None if raw_limit >= _V1_UNLIMITED else raw_limit
                usage = _read_int(current_path, label=f"cgroup v{version} memory usage")
                ancestors.append({
                    "path": str(directory),
                    "limit_bytes": limit,
                    "current_bytes": usage,
                    "headroom_bytes": None if limit is None else max(0, limit - usage),
                })
            finite = [entry["headroom_bytes"] for entry in ancestors if entry["headroom_bytes"] is not None]
            return {
                "cgroup_version": version,
                "cgroup_controller": controller,
                "cgroup_membership": membership,
                "cgroup_mount_root": mount["root"],
                "cgroup_mountpoint": mount["mountpoint"],
                "cgroup_visible_ancestor_count": len(ancestors),
                "cgroup_visible_ancestors": ancestors,
                "cgroup_visible_headroom_bytes": min(finite) if finite else None,
                "cgroup_unlimited": not finite,
                "cgroup_namespace_root_visible": namespace_rooted,
                "cgroup_visibility": "namespace_or_mount_boundary" if namespace_rooted or mount["root"] != "/" else "mount_visible_namespace_not_proven",
                "cgroup_boundary_note": (
                    "Only ancestors visible beneath this mount root were sampled; namespace-hidden limits may exist."
                    if namespace_rooted or mount["root"] != "/" else "Sampler reports the visible mount hierarchy only; a namespace may hide limits outside it."
                ),
            }
        except ResourceGateError as exc:
            failures.append({"message": str(exc), **exc.details})
    raise ResourceGateError(
        "cannot map or read the process memory cgroup",
        {"error_code": "cgroup_memory_unavailable", "version": version, "attempts": failures},
    )


def _linux_priority_status() -> tuple[int | None, bool]:
    getter = getattr(os, "getpriority", None)
    selector = getattr(os, "PRIO_PROCESS", None)
    if getter is None or selector is None:
        return None, False
    try:
        value = int(getter(selector, 0))
    except OSError:
        return None, False
    return value, value >= 5


def _cpu_context(cgroup: dict[str, Any] | None = None) -> dict[str, Any]:
    cpu_count = os.cpu_count()
    affinity: list[int] | None = None
    affinity_error = None
    getter = getattr(os, "sched_getaffinity", None)
    if getter is None:
        affinity_error = "sched_getaffinity_unavailable"
    else:
        try:
            affinity = sorted(int(cpu) for cpu in getter(0))
        except OSError as exc:
            affinity_error = f"sched_getaffinity_failed: {exc}"
    quotas: list[dict[str, Any]] = []
    if cgroup:
        if cgroup["cgroup_version"] == 2:
            for entry in cgroup["cgroup_visible_ancestors"]:
                path = Path(entry["path"]) / "cpu.max"
                try:
                    parts = path.read_text(encoding="ascii").split()
                    if len(parts) != 2:
                        raise ValueError("expected quota period")
                    quota = None if parts[0] == "max" else int(parts[0])
                    period = int(parts[1])
                    if period <= 0 or (quota is not None and quota < 0):
                        raise ValueError("invalid quota or period")
                    quotas.append({"path": str(path), "quota_us": quota, "period_us": period, "cpu_limit": None if quota is None else quota / period})
                except (OSError, UnicodeError, ValueError) as exc:
                    quotas.append({"path": str(path), "status": "unavailable", "reason": str(exc)})
        else:
            quotas.append({"status": "unavailable", "reason": "cpu v1 hierarchy is not mapped by the memory sampler"})
    else:
        quotas.append({"status": "unavailable", "reason": "cgroup hierarchy unavailable"})
    finite = [entry["cpu_limit"] for entry in quotas if entry.get("cpu_limit") is not None]
    return {
        "host_cpu_count": cpu_count,
        "process_cpu_affinity": affinity,
        "process_cpu_affinity_count": len(affinity) if affinity is not None else None,
        "process_cpu_affinity_status": "available" if affinity is not None else affinity_error,
        "visible_cgroup_cpu_quotas": quotas,
        "visible_cgroup_cpu_limit": min(finite) if finite else None,
    }


def set_process_priority(*, platform_name: str | None = None) -> dict[str, Any]:
    """Lower only the dedicated runner process and verify the effective value."""
    name = platform_name or sys.platform
    if name.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.SetPriorityClass.restype = wintypes.BOOL
            kernel.GetPriorityClass.argtypes = [wintypes.HANDLE]
            kernel.GetPriorityClass.restype = wintypes.DWORD
            handle = kernel.GetCurrentProcess()
            requested = 0x00004000
            if not kernel.SetPriorityClass(handle, requested):
                raise OSError(ctypes.get_last_error(), "SetPriorityClass failed")
            actual = int(kernel.GetPriorityClass(handle))
            if actual != requested:
                raise OSError("Windows priority class did not match request")
            return {"priority_kind": "windows_priority_class", "priority_value": actual, "below_normal_priority_set": True}
        except (ImportError, AttributeError, OSError) as exc:
            raise ResourceGateError(
                "cannot set and verify Windows BelowNormal priority",
                {"error_code": "priority_unavailable", "platform": "windows", "message": str(exc)},
            ) from exc
    if name.startswith("linux"):
        try:
            before, _ = _linux_priority_status()
            if before is None:
                raise OSError("getpriority is unavailable")
            if before < 5:
                os.nice(5 - before)
            actual, below_normal = _linux_priority_status()
            if actual is None or not below_normal:
                raise OSError("Linux niceness is below required +5")
            return {"priority_kind": "unix_nice", "priority_value": actual, "below_normal_priority_set": True}
        except OSError as exc:
            raise ResourceGateError(
                "cannot set and verify Linux niceness",
                {"error_code": "priority_unavailable", "platform": "linux", "message": str(exc)},
            ) from exc
    raise ResourceGateError(
        "unsupported platform for resource gate",
        {"error_code": "platform_unsupported", "platform": name},
    )


def sample_resources(
    *,
    platform_name: str | None = None,
    proc_root: Path = Path("/proc"),
    sys_root: Path = Path("/sys"),
    page_size_bytes: int | None = None,
) -> dict[str, Any]:
    """Return one resource sample. Linux host-only fallback is deliberately forbidden."""
    name = platform_name or sys.platform
    now = datetime.now(UTC).isoformat()
    if name.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
            kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
            kernel.GetPriorityClass.argtypes = [wintypes.HANDLE]
            kernel.GetPriorityClass.restype = wintypes.DWORD
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            memory = MemoryStatus()
            memory.dwLength = ctypes.sizeof(memory)
            if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
                raise ctypes.WinError(ctypes.get_last_error())
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                raise ctypes.WinError(ctypes.get_last_error())
            priority_class = int(kernel.GetPriorityClass(kernel.GetCurrentProcess()))
            thread_limits = {name: os.environ.get(name) for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")}
            return {
                "sampled": True, "sampled_at_utc": now, "platform": platform.platform(),
                "host_available_bytes": int(memory.ullAvailPhys),
                "effective_available_bytes": int(memory.ullAvailPhys),
                "process_rss_bytes": int(counters.WorkingSetSize),
                "below_normal_priority_set": priority_class == 0x00004000,
                "priority_kind": "windows_priority_class", "priority_value": priority_class,
                "blas_threads": os.environ.get("OPENBLAS_NUM_THREADS"),
                "thread_limits": thread_limits,
                "cpu_context": _cpu_context(),
                "container_limit_coverage": "Windows job/container limits are not measured by this sampler.",
            }
        except (ImportError, AttributeError, OSError) as exc:
            raise ResourceGateError(
                "Windows resource sampling failed",
                {"error_code": "resource_measurement_unavailable", "platform": "windows", "message": str(exc)},
            ) from exc
    if name.startswith("linux"):
        host_available, rss = _proc_memory(proc_root, page_size_bytes=page_size_bytes)
        cgroup = resolve_cgroup_memory(proc_root, sys_root)
        cgroup_headroom = cgroup["cgroup_visible_headroom_bytes"]
        effective_available = host_available if cgroup_headroom is None else min(host_available, cgroup_headroom)
        priority, priority_ok = _linux_priority_status()
        return {
            "sampled": True, "sampled_at_utc": now, "platform": platform.platform(),
            "host_available_bytes": host_available, "effective_available_bytes": effective_available,
            "process_rss_bytes": rss, "below_normal_priority_set": priority_ok,
            "priority_kind": "unix_nice", "priority_value": priority,
            "blas_threads": os.environ.get("OPENBLAS_NUM_THREADS"),
            "thread_limits": {name: os.environ.get(name) for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")},
            **cgroup,
            "cpu_context": _cpu_context(cgroup),
        }
    raise ResourceGateError(
        "unsupported platform for resource sampling",
        {"error_code": "platform_unsupported", "platform": name},
    )


def enforce_resource_gate(
    sample: dict[str, Any], *, min_free_bytes: int, max_working_set_bytes: int
) -> dict[str, Any]:
    """Enforce thresholds and required measurements; unknown values always fail."""
    required = ("sampled", "effective_available_bytes", "process_rss_bytes", "below_normal_priority_set", "blas_threads", "thread_limits")
    missing = [key for key in required if key not in sample or sample[key] is None]
    if missing or sample.get("sampled") is not True:
        raise ResourceGateError(
            "resource sample is incomplete",
            {"error_code": "resource_sample_incomplete", "missing": missing, "sample": sample},
        )
    if sample["blas_threads"] != "1" or any(sample["thread_limits"].get(name) != "1" for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")) or sample["below_normal_priority_set"] is not True:
        raise ResourceGateError(
            "thread or process priority gate failed",
            {"error_code": "resource_policy_failed", "sample": sample},
        )
    failures = {}
    if int(sample["effective_available_bytes"]) < min_free_bytes:
        failures["effective_available_bytes"] = sample["effective_available_bytes"]
    if int(sample["process_rss_bytes"]) > max_working_set_bytes:
        failures["process_rss_bytes"] = sample["process_rss_bytes"]
    if failures:
        raise ResourceGateError(
            "resource thresholds exceeded",
            {"error_code": "resource_threshold_failed", "thresholds": {"min_free_bytes": min_free_bytes, "max_working_set_bytes": max_working_set_bytes}, "actual": failures},
        )
    return {"passed": True, "thresholds": {"min_free_bytes": min_free_bytes, "max_working_set_bytes": max_working_set_bytes}, "sample": sample}
