"""Un-timed functional checks for the paired material-locator fixture.

Each check runs in a fresh child with a minimal environment. It never invokes the
acceptance CLI or its warmup/timed matrix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NETWORK_TOOLS = ROOT / "artifacts" / "kb-lifecycle-concurrency-2026-10-24" / "tools"
PRIORITY_TOOLS = ROOT / "artifacts" / "staging-recovery-2026-10-25" / "verification" / "tools"


def _run_child(tmp_path: Path, source: str) -> None:
    """Run a functional probe with only private paths and no inherited credentials."""
    child_root = tmp_path / "child"
    child_root.mkdir()
    private_home = child_root / "home"
    private_temp = child_root / "tmp"
    private_home.mkdir()
    private_temp.mkdir()
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "VIRTUAL_ENV")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update(
        {
            "PYTHONPATH": os.pathsep.join((str(ROOT), str(NETWORK_TOOLS), str(PRIORITY_TOOLS))),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(private_home),
            "USERPROFILE": str(private_home),
            "TEMP": str(private_temp),
            "TMP": str(private_temp),
            "TMPDIR": str(private_temp),
            "RAG_USERS_DB_PATH": str(child_root / "guard-users.sqlite3"),
            "RAG_MEMORY_DB_PATH": str(child_root / "memory.sqlite3"),
            "RAG_ANALYSIS_DB_PATH": str(child_root / "analysis.sqlite3"),
            "RAG_WIKI_DB_PATH": str(child_root / "wiki.sqlite3"),
            "RAG_BM25_DB_PATH": str(child_root / "bm25.sqlite3"),
            "RAG_EVOLUTION_DB_PATH": str(child_root / "evolution.sqlite3"),
            "RAG_QDRANT_PATH": str(child_root / "qdrant"),
            "RAG_DOCUMENT_VERSIONS_ROOT": str(child_root / "document_versions"),
            "RAG_STRUCTURED_DATASETS_ROOT": str(child_root / "structured_datasets"),
            "RAG_AUTH_ENABLED": "false",
            "RAG_QDRANT_URL": "",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )
    command = [sys.executable, "-c", source, str(ROOT), str(child_root)]
    stdout_path = tmp_path / "child.stdout.log"
    stderr_path = tmp_path / "child.stderr.log"
    record_path = tmp_path / "child-process.json"
    timed_out = False
    with (
        stdout_path.open("w", encoding="utf-8", newline="\n") as stdout_file,
        stderr_path.open("w", encoding="utf-8", newline="\n") as stderr_file,
    ):
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            stdout=stdout_file,
            stderr=stderr_file,
        )
        child_pid = process.pid
        try:
            native_exit = process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            native_exit = process.wait()
    process_record = {
        "pid": child_pid,
        "native_exit": native_exit,
        "timed_out": timed_out,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    record_path.write_text(json.dumps(process_record, indent=2) + "\n", encoding="utf-8")
    stdout = stdout_path.read_text(encoding="utf-8")
    stderr = stderr_path.read_text(encoding="utf-8")
    assert native_exit == 0 and not timed_out, (
        f"isolated functional child failed: {process_record}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )


_PROFILE_CHILD = r"""
import json, os, sys
from pathlib import Path

root, scratch = Path(sys.argv[1]), Path(sys.argv[2])
from offline_network_guard import _guarded_connect, _guarded_connect_ex
import socket
socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
def _block_network(*_args, **_kwargs):
    raise RuntimeError("isolated_functional_test_network_blocked")
socket.socket.connect = _block_network
socket.socket.connect_ex = _block_network
socket.socket.sendto = _block_network
if hasattr(socket.socket, "sendmsg"):
    socket.socket.sendmsg = _block_network
socket.create_connection = _block_network
socket.getaddrinfo = _block_network
socket.gethostbyname = _block_network
socket.gethostbyname_ex = _block_network
socket.gethostbyaddr = _block_network
socket.getnameinfo = _block_network
import staging_test_priority
if sys.platform == "win32" and staging_test_priority.PRIORITY_CLASS != 0x4000:
    raise AssertionError("child process is not BelowNormal")
if sys.platform.startswith("linux"):
    before = os.getpriority(os.PRIO_PROCESS, 0)
    if before < 5:
        os.nice(5 - before)
    if os.getpriority(os.PRIO_PROCESS, 0) < 5:
        raise AssertionError("child process niceness is below +5")

from scripts.material_locator_acceptance_support import (
    disable_dotenv_loading, install_repository_counters, logical_database_hash,
    install_sqlite_path_audit, new_counter_store, repository_counter_snapshot,
    database_counter_snapshot,
)
disable_dotenv_loading()
install_sqlite_path_audit(scratch)
from scripts import paired_material_locator_acceptance as acceptance
from scripts.paired_material_locator_acceptance import build_synthetic_fixture, snapshot
from scripts.material_locator_acceptance_support import semantic_probe
from rag.materials.locator import MaterialLocator
from rag.materials.models import MaterialLocationQuery

profiles = {
    "two-kb-user": {
        "distribution": "two-kb", "admin": False,
        "expected_ids": [1, 2, 3, 4, 5, 6, 8],
        "model_order": [0, 2, 4, 1, 3, 5, 7],
        "construction": {"documents": 8, "owner": 7, "private": 8, "public": 0, "shared": 0, "owner_hidden": 1, "organization_allowed": 0, "organization_denied": 0, "kbs": 2},
    },
    "many-kb-user": {
        "distribution": "many-kb", "admin": False,
        "expected_ids": [1, 2, 3, 4, 5, 6, 8],
        "model_order": [0, 1, 2, 3, 4, 5, 7],
        "construction": {"documents": 8, "owner": 7, "private": 8, "public": 0, "shared": 0, "owner_hidden": 1, "organization_allowed": 0, "organization_denied": 0, "kbs": 8},
    },
    "mixed-acl": {
        "distribution": "mixed-acl", "admin": False,
        "expected_ids": [3, 4, 6, 7, 8],
        "model_order": [2, 3, 5, 6, 7],
        "construction": {"documents": 8, "owner": 7, "private": 4, "public": 2, "shared": 2, "owner_hidden": 1, "organization_allowed": 1, "organization_denied": 2, "kbs": 4},
    },
    "two-kb-admin": {
        "distribution": "two-kb", "admin": True,
        "expected_ids": [1, 2, 3, 4, 5, 6, 7, 8],
        "model_order": [0, 2, 4, 6, 1, 3, 5, 7],
        "construction": {"documents": 8, "owner": 8, "private": 8, "public": 0, "shared": 0, "owner_hidden": 0, "organization_allowed": 0, "organization_denied": 0, "kbs": 2},
    },
}
counter_methods = (
    "list_material_documents_with_current_version",
    "list_material_document_kb_ids",
    "list_material_documents_for_kb",
    "list_material_documents_for_access_plan",
)
results = {}
for name, spec in profiles.items():
    db_path = scratch / (name + ".sqlite3")
    db, user, _fixture_query = build_synthetic_fixture(
        db_path, 8, spec["distribution"], spec["admin"]
    )
    try:
        expected_ids = set(spec["expected_ids"])
        before = logical_database_hash(db)
        summary = db._acceptance_fixture_summary
        assert summary["profile_version"] == 2, (name, summary)
        assert summary["construction_attributes"] == spec["construction"], (name, summary)
        expected_hidden = sorted(set(range(1, 9)) - expected_ids)
        assert summary["effective_acl"] == {
            "visible_document_ids": sorted(expected_ids),
            "hidden_document_ids": expected_hidden,
            "visible_count": len(expected_ids),
            "hidden_count": len(expected_hidden),
        }, (name, summary)

        # Independent eight-row contract: denied-org rows 1 and 2 are public/shared.
        if name == "mixed-acl":
            with db.locked_read() as conn:
                denied = conn.execute(
                    "SELECT id, scope FROM document_permissions "
                    "WHERE kb_id='kb-org-deny' ORDER BY id"
                ).fetchall()
            assert [(int(row[0]), row[1]) for row in denied] == [(1, "public"), (2, "shared")]

        baseline_class = acceptance.load_frozen_baseline()
        query = MaterialLocationQuery(text="paired", subject=None, top_k=10)
        old_value = snapshot(baseline_class(db), query, user)
        new_value = snapshot(MaterialLocator(db), query, user)
        assert old_value == new_value, name
        assert {int(row[0]) for row in new_value[0]} == expected_ids, (name, new_value[0])
        assert new_value[1] == "\n".join(
            f"synthetic-{index:06d}.txt Synthetic/Acceptance memo"
            for index in spec["model_order"]
        ), name
        assert all(len(row) == 11 for row in new_value[0]), name
        assert all(row[9:] == [1.0, "fts"] for row in new_value[0]), name

        version_ids = []
        with db.locked_read() as conn:
            for document_id in sorted(expected_ids):
                row = conn.execute(
                    "SELECT current_version_id FROM document_permissions WHERE id=?",
                    (document_id,),
                ).fetchone()
                assert row is not None and row[0] is not None
                version_ids.append(int(row[0]))
        semantic_query = MaterialLocationQuery(text="paired", subject=None, top_k=1)
        expected_top1_old = snapshot(baseline_class(db), semantic_query, user)
        expected_top1_new = snapshot(MaterialLocator(db), semantic_query, user)
        assert expected_top1_old == expected_top1_new, name
        assert {int(row[0]) for row in expected_top1_new[0]} <= expected_ids, name
        old_semantic = semantic_probe(baseline_class, db, semantic_query, user)
        new_semantic = semantic_probe(MaterialLocator, db, semantic_query, user)
        expected_call = [{
            "text": "paired", "allowed_version_ids": sorted(version_ids), "top_k": 1,
        }]
        assert old_semantic == new_semantic, name
        assert old_semantic["calls"] == expected_call, (name, old_semantic["calls"])
        assert old_semantic["result"] == expected_top1_old[0], name
        assert old_semantic["model_input"] == new_value[1]
        assert len(old_semantic["calls"][0]["allowed_version_ids"]) > semantic_query.top_k

        store = new_counter_store()
        active = __import__("threading").local()
        active.name, active.phase = None, None
        acceptance._install_sql_trace(db, store, active)
        for implementation in ("old", "new"):
            install_repository_counters(db.material_repo, store, implementation, active)
        for implementation in ("old", "new"):
            active.name, active.phase = implementation, "functional"
            repo = db.material_repo
            all_rows = repo.list_material_documents_with_current_version()
            kb_ids = repo.list_material_document_kb_ids()
            per_kb_rows = repo.list_material_documents_for_kb(
                kb_ids[0], apply_legacy_acl=True, user_id=int(user["id"])
            )
            plan_rows = repo.list_material_documents_for_access_plan(
                [(kb_ids[0], True)], user_id=int(user["id"])
            )
            assert len(all_rows) == 8
            assert kb_ids
            assert len(per_kb_rows) == len(plan_rows)
        active.name, active.phase = None, None

        counts = repository_counter_snapshot(store)
        database_counts = database_counter_snapshot(store)
        for implementation in ("old", "new"):
            for method in counter_methods:
                cell = counts[implementation][method]["functional"]
                assert cell["calls"] == 1, (name, implementation, method, cell)
                assert cell["returned_list_rows"] >= 0
            assert store["sql_by_phase"][implementation]["functional"], (
                name, implementation, "missing functional SQL trace"
            )
            assert database_counts["query_counts"][implementation]["all"] > 0

        after = logical_database_hash(db)
        assert before == after, name
        results[name] = {
            "expected_ids": sorted(expected_ids),
            "hash": before["sha256"],
            "old_new_snapshot_equal": True,
            "semantic_full_ids_top_k_1": True,
            "four_real_repository_methods_counted": True,
        }
    finally:
        db.close()
print(json.dumps(results, sort_keys=True))
"""


def test_tiny_real_sqlite_profiles_match_full_locator_contract_without_timing(tmp_path):
    _run_child(tmp_path, _PROFILE_CHILD)


_CLOSE_CHILD = r"""
import json, os, sys
from pathlib import Path
from unittest.mock import patch

root, scratch = Path(sys.argv[1]), Path(sys.argv[2])
from offline_network_guard import _guarded_connect, _guarded_connect_ex
import socket
socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
def _block_network(*_args, **_kwargs):
    raise RuntimeError("isolated_functional_test_network_blocked")
socket.socket.connect = _block_network
socket.socket.connect_ex = _block_network
socket.socket.sendto = _block_network
if hasattr(socket.socket, "sendmsg"):
    socket.socket.sendmsg = _block_network
socket.create_connection = _block_network
socket.getaddrinfo = _block_network
socket.gethostbyname = _block_network
socket.gethostbyname_ex = _block_network
socket.gethostbyaddr = _block_network
socket.getnameinfo = _block_network
import staging_test_priority
if sys.platform == "win32" and staging_test_priority.PRIORITY_CLASS != 0x4000:
    raise AssertionError("child process is not BelowNormal")
if sys.platform.startswith("linux"):
    before = os.getpriority(os.PRIO_PROCESS, 0)
    if before < 5:
        os.nice(5 - before)
    if os.getpriority(os.PRIO_PROCESS, 0) < 5:
        raise AssertionError("child process niceness is below +5")

from scripts.material_locator_acceptance_support import (
    disable_dotenv_loading, install_sqlite_path_audit, load_frozen_baseline,
)
disable_dotenv_loading()
install_sqlite_path_audit(scratch)
from scripts import paired_material_locator_acceptance as acceptance
from rag.materials.locator import MaterialLocator

created = []
real_builder = acceptance.build_synthetic_fixture
def capture_builder(*args, **kwargs):
    value = real_builder(*args, **kwargs)
    created.append(value[0])
    return value
def fail_diagnostic(*_args, **_kwargs):
    raise RuntimeError("injected_first_diagnostic_failure")
def forbidden(*_args, **_kwargs):
    raise AssertionError("timed/resource path must not be reached")

acceptance.build_synthetic_fixture = capture_builder
acceptance.snapshot = fail_diagnostic
case_spec = {
    "case": "two-kb-user", "distribution": "two-kb", "admin": False,
    "requested_n": 8, "actual_n": 8, "concurrency": 1,
    "rounds": 3, "warmups": 3, "samples_per_worker_per_round": 10,
    "expected_pairs": 30, "directory": "close-probe", "matrix_group": "functional",
}
case_dir = scratch / "case"
output = scratch / "failure.json"
progress = {"runs": []}
with patch.object(acceptance, "ThreadPoolExecutor", forbidden), \
     patch.object(acceptance, "_timed_worker", forbidden), \
     patch.object(acceptance.time, "perf_counter_ns", forbidden), \
     patch.object(acceptance, "sample_resources", forbidden), \
     patch.object(acceptance, "enforce_resource_gate", forbidden):
    try:
        acceptance.run_case(
            case_spec=case_spec, case_dir=case_dir,
            baseline_class=load_frozen_baseline(), new_class=MaterialLocator,
            progress=progress, output_path=output,
            thresholds={"min_free_bytes": 1, "max_working_set_bytes": 1},
        )
    except RuntimeError as exc:
        assert str(exc) == "injected_first_diagnostic_failure"
        acceptance._record_run_failure(progress, exc)
        acceptance.write_json_atomic(output, progress)
    else:
        raise AssertionError("diagnostic injection should abort run_case")
assert len(created) == 1
assert created[0].cm._closed is True
persisted = json.loads(output.read_text(encoding="utf-8"))
assert persisted["status"] == "failed_with_partial"
assert persisted["error"]["message"] == "injected_first_diagnostic_failure"
assert persisted["in_progress"]["last_round_status"] == "case_fixture_ready"
assert "diagnostic_probe" not in persisted["in_progress"]
print(json.dumps({"db_closed": True, "timed_or_resource_path_reached": False, "status": persisted["status"]}))
"""


def test_first_diagnostic_failure_closes_fixture_before_timing_or_resource_sampling(tmp_path):
    _run_child(tmp_path, _CLOSE_CHILD)
