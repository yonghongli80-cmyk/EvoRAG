"""Explicit freeze/check/run CLI for the synthetic material locator acceptance."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from scripts.material_locator_acceptance_support import (
    BASELINE,
    ROOT,
    AcceptanceError,
    append_sql_trace,
    configure_isolated_context,
    create_source_freeze,
    database_counter_snapshot,
    install_offline_network_guard,
    install_repository_counters,
    install_sqlite_path_audit,
    load_frozen_baseline,
    logical_database_hash,
    new_counter_store,
    repository_counter_snapshot,
    sanitize_process_environment,
    semantic_probe,
    source_hashes,
    validate_output_path,
    verify_source_freeze,
    write_json_atomic,
    write_json_new,
)
from scripts.material_locator_resources import (
    ResourceGateError,
    enforce_resource_gate,
    sample_resources,
    set_process_priority,
)

CORE_COUNTS = (1000, 5000, 10000)
CORE_WORKERS = (1, 2)
CORE_CASES = (
    ("two-kb-user", "two-kb", False),
    ("many-kb-user", "many-kb", False),
    ("two-kb-admin", "two-kb", True),
)


def case_directory_name(*, requested_n: int, actual_n: int, case: str, concurrency: int) -> str:
    if requested_n < 1 or actual_n < 1 or concurrency not in (1, 2):
        raise ValueError("invalid acceptance case path components")
    safe_case = "".join(char if char.isalnum() or char in "-_" else "-" for char in case)
    if not safe_case:
        raise ValueError("case name is empty")
    return f"{safe_case}-requestedN{requested_n}-N{actual_n}-C{concurrency}"


def core_case_matrix(
    counts: tuple[int, ...] = CORE_COUNTS,
    workers: tuple[int, ...] = CORE_WORKERS,
    *,
    rounds: int = 3,
    warmups: int = 3,
    samples: int = 10,
) -> list[dict[str, Any]]:
    if len(set(counts)) != len(counts) or len(set(workers)) != len(workers):
        raise ValueError("counts and workers must be unique")
    if any(count < 1 for count in counts) or rounds < 1 or warmups < 0 or samples < 1:
        raise ValueError("invalid matrix dimensions")
    cases: list[dict[str, Any]] = []
    for requested_n in counts:
        for concurrency in workers:
            if concurrency not in (1, 2):
                raise ValueError("concurrency must be 1 or 2")
            for name, distribution, admin in CORE_CASES:
                actual_n = requested_n if name == "two-kb-user" else min(requested_n, 100)
                cases.append({
                    "case": name,
                    "distribution": distribution,
                    "admin": admin,
                    "requested_n": requested_n,
                    "actual_n": actual_n,
                    "concurrency": concurrency,
                    "rounds": rounds,
                    "warmups": warmups,
                    "samples_per_worker_per_round": samples,
                    "expected_pairs": rounds * samples * concurrency,
                    "directory": case_directory_name(requested_n=requested_n, actual_n=actual_n, case=name, concurrency=concurrency),
                    "matrix_group": "core",
                })
    return cases


def acl_extension_matrix(
    workers: tuple[int, ...] = CORE_WORKERS,
    *,
    rounds: int = 3,
    warmups: int = 3,
    samples: int = 10,
) -> list[dict[str, Any]]:
    if len(set(workers)) != len(workers) or rounds < 1 or warmups < 0 or samples < 1:
        raise ValueError("invalid ACL extension dimensions")
    cases = []
    for concurrency in workers:
        if concurrency not in (1, 2):
            raise ValueError("concurrency must be 1 or 2")
        cases.append({
            "case": "acl-extension",
            "distribution": "mixed-acl",
            "admin": False,
            "requested_n": 100,
            "actual_n": 100,
            "concurrency": concurrency,
            "rounds": rounds,
            "warmups": warmups,
            "samples_per_worker_per_round": samples,
            "expected_pairs": rounds * samples * concurrency,
            "directory": case_directory_name(requested_n=100, actual_n=100, case="acl-extension", concurrency=concurrency),
            "matrix_group": "acl_extension",
        })
    return cases


def round_order(round_id: int) -> list[str]:
    if round_id < 0:
        raise ValueError("round_id cannot be negative")
    return ["old", "new"] if round_id % 2 == 0 else ["new", "old"]


def _serialize_rows(rows: list[Any]) -> list[list[Any]]:
    return [[
        row.document_id,
        row.document_version_id,
        row.filename,
        row.logical_path,
        row.document_type,
        row.updated_at,
        row.ocr_warning,
        row.citation.location.kind,
        row.citation.location.value,
        row.rank,
        row.source,
    ] for row in rows]


def snapshot(locator: Any, query: Any, user: Any) -> tuple[list[list[Any]], str]:
    rows = locator.locate(query, user)
    return _serialize_rows(rows), locator.model_input


def _create_org_policy(db: Any, user_id: int, *, allow: bool) -> str:
    org = "ORG-ALLOW" if allow else "ORG-DENY"
    kb_id = f"kb-{org.lower()}"
    with db.locked_transaction() as connection:
        connection.execute("INSERT INTO org_units(code,parent_code,name,kind) VALUES(?,NULL,?,'team')", (org, org))
        connection.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title,active) VALUES(?,?,?,?,1)",
            (f"PERSON-{org}", user_id, "synthetic", "tester"),
        )
        connection.execute("INSERT INTO org_memberships(person_code,org_code,is_primary) VALUES(?,?,1)", (f"PERSON-{org}", org))
        connection.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES(?,?,'organization')", (kb_id, org))
    return kb_id


def build_synthetic_fixture(path: Path, count: int, distribution: str, admin_case: bool = False) -> tuple[Any, dict[str, Any], Any]:
    from rag.materials.models import MaterialLocationQuery
    from rag.user_db import UserDB

    db = UserDB(str(path))
    try:
        db.create_material_schema()
        owner_id = db.create_user("bench-owner", "Synthetic-Password-123!")
        hidden_owner_id = db.create_user("bench-hidden", "Synthetic-Password-123!")
        db.set_user_admin(owner_id, True) if admin_case else None
        if distribution == "mixed-acl":
            allowed_org_kb = _create_org_policy(db, owner_id, allow=True)
            denied_org_kb = _create_org_policy(db, hidden_owner_id, allow=False)
        else:
            allowed_org_kb = denied_org_kb = None

        fixture_counts = {"documents": 0, "owner": 0, "public": 0, "shared": 0, "hidden": 0, "organization_allowed": 0, "organization_denied": 0, "kbs": set()}
        for index in range(count):
            if distribution == "many-kb":
                kb_id = f"kb-{index:06d}"
            elif distribution == "mixed-acl":
                kb_id = ("kb-a", "kb-b", allowed_org_kb, denied_org_kb)[index % 4]
            else:
                kb_id = "kb-a" if index % 2 == 0 else "kb-b"
            is_hidden = (distribution == "mixed-acl" and index % 5 == 4) or (distribution != "mixed-acl" and not admin_case and index % 7 == 6)
            if distribution == "mixed-acl" and index % 4 == 2:
                kb_id = allowed_org_kb
            elif distribution == "mixed-acl" and index % 4 == 3:
                kb_id = denied_org_kb
            subject_owner = hidden_owner_id if is_hidden else owner_id
            scope = "public" if distribution == "mixed-acl" and index % 5 == 0 else ("shared" if distribution == "mixed-acl" and index % 5 == 1 else "private")
            filename = f"synthetic-{index:06d}.txt"
            document_id = db.create_document_permission(filename, kb_id, subject_owner, scope=scope)
            if scope == "shared":
                db.share_document(document_id, owner_id, subject_owner, "view")
            with db.locked_transaction() as connection:
                connection.execute(
                    "UPDATE document_permissions SET subject_user_id=?, document_type=?, logical_path=?, updated_at=? WHERE id=?",
                    (owner_id, "memo", "Synthetic/Acceptance", 1750000000.0 + index, document_id),
                )
            version_id = db.create_document_version(document_id, filename, f"/synthetic/{index}", f"hash-{index}", subject_owner)
            db.activate_document_version(document_id, version_id, kb_id, filename)
            db.insert_material_search_fts(document_id, version_id, filename, "Synthetic/Acceptance", "memo", "[]", "paired synthetic benchmark")
            fixture_counts["documents"] += 1
            fixture_counts["kbs"].add(kb_id)
            if is_hidden:
                fixture_counts["hidden"] += 1
            elif scope == "public":
                fixture_counts["public"] += 1
            elif scope == "shared":
                fixture_counts["shared"] += 1
            else:
                fixture_counts["owner"] += 1
            if kb_id == allowed_org_kb:
                fixture_counts["organization_allowed"] += 1
            elif kb_id == denied_org_kb:
                fixture_counts["organization_denied"] += 1

        user = db.get_user_by_id(owner_id)
        user = {**user, "is_admin": bool(admin_case)}
        query = MaterialLocationQuery(text="paired", subject=None, top_k=10)
        db._acceptance_fixture_summary = {**fixture_counts, "kbs": len(fixture_counts["kbs"])}
        return db, user, query
    except BaseException:
        db.close()
        raise


def _install_sql_trace(db: Any, store: dict[str, Any], active_label: threading.local) -> None:
    original = db.cm.get_read_connection

    def traced_connection(*args: Any, **kwargs: Any) -> Any:
        connection = original(*args, **kwargs)
        connection.set_trace_callback(lambda statement: append_sql_trace(store, active_label, statement))
        from scripts.material_locator_acceptance_support import _ConnectionCounter
        return _ConnectionCounter(connection, store, active_label)

    db.cm.get_read_connection = traced_connection


def _prepare_locator_classes(db: Any, baseline_class: Any, new_class: Any, store: dict[str, Any], active_label: threading.local) -> None:
    repo = db.material_repo
    for implementation in ("old", "new"):
        install_repository_counters(repo, store, implementation, active_label)


def _timed_worker(
    worker_id: int,
    implementation: str,
    locator_class: Any,
    db: Any,
    query: Any,
    user: Any,
    sample_count: int,
    active_label: threading.local,
) -> tuple[int, list[tuple[list[list[Any]], str, int]]]:
    locator = locator_class(db)
    outputs = []
    for _sample in range(sample_count):
        active_label.name = implementation
        active_label.phase = "timed_measurement"
        started = time.perf_counter_ns()
        result = snapshot(locator, query, user)
        elapsed = time.perf_counter_ns() - started
        active_label.name = None
        active_label.phase = None
        outputs.append((result[0], result[1], elapsed))
    return worker_id, outputs


def _legacy_counter_fields(counters: dict[str, Any]) -> dict[str, Any]:
    phases = ("diagnostic_probe", "semantic_probe", "warmup", "timed_measurement")
    wrapped_methods: dict[str, str] = {}
    returned_rows: dict[str, Any] = {"old": {}, "new": {}}
    calls: dict[str, Any] = {"old": {}, "new": {}}
    for implementation, methods in counters.items():
        for method, values in methods.items():
            wrapped_methods[method] = values["row_kind"]
            returned_rows[implementation][method] = {
                "row_kind": values["row_kind"],
                **{phase: values[phase]["returned_list_rows"] for phase in phases},
            }
            calls[implementation][method] = {phase: values[phase]["calls"] for phase in phases}
    return {
        "wrapped_methods": wrapped_methods,
        "wrapped_repository_return_rows": returned_rows,
        "wrapped_repository_calls": calls,
        "counted_phases": list(phases),
        "row_count_scope": "Returned list lengths from the four wrapped repository methods only; excludes other SQLite cursor/query rows and is not a total-load count.",
    }


def run_case(
    *, case_spec: dict[str, Any], case_dir: Path, baseline_class: Any, new_class: Any,
    progress: dict[str, Any], output_path: Path, thresholds: dict[str, int],
) -> dict[str, Any]:
    case_dir.mkdir(parents=True, exist_ok=False)
    database_path = case_dir / "users.sqlite3"
    db, user, query = build_synthetic_fixture(database_path, case_spec["actual_n"], case_spec["distribution"], case_spec["admin"])
    try:
        before_hash = logical_database_hash(db)
        fixture_summary = db._acceptance_fixture_summary
        progress["in_progress"] = {
            **case_spec,
            "last_round_status": "case_fixture_ready",
            "db_hash_before": before_hash,
            "fixture_summary": fixture_summary,
            "completed_rounds": 0,
            "samples": [],
            "resource_checkpoints": [],
        }
        write_json_atomic(output_path, progress)
        store = new_counter_store()
        active_label = threading.local()
        _install_sql_trace(db, store, active_label)
        _prepare_locator_classes(db, baseline_class, new_class, store, active_label)
        from rag.materials.locator import MaterialLocator

        if MaterialLocator is not new_class:
            raise AcceptanceError("current locator class changed after import", code="source_import_mismatch")
        diagnostics = {}
        for implementation, locator_class in (("old", baseline_class), ("new", new_class)):
            active_label.name = implementation
            active_label.phase = "diagnostic_probe"
            diagnostics[implementation] = snapshot(locator_class(db), query, user)
        active_label.name = None
        active_label.phase = None
        progress["in_progress"]["diagnostic_probe"] = diagnostics
        write_json_atomic(output_path, progress)
        if diagnostics["old"] != diagnostics["new"]:
            raise AssertionError("diagnostic full output/model_input mismatch")

        semantic = {}
        for implementation, locator_class in (("old", baseline_class), ("new", new_class)):
            active_label.name = implementation
            active_label.phase = "semantic_probe"
            semantic[implementation] = semantic_probe(locator_class, db, query, user)
        active_label.name = None
        active_label.phase = None
        progress["in_progress"]["semantic_probe"] = semantic
        write_json_atomic(output_path, progress)
        if semantic["old"] != semantic["new"]:
            raise AssertionError("semantic probe/result mismatch")

        def warm_worker(worker_id: int) -> tuple[int, tuple[list[list[Any]], str]]:
            old_locator, new_locator = baseline_class(db), new_class(db)
            last = ([], "")
            for _ in range(case_spec["warmups"]):
                active_label.name, active_label.phase = "old", "warmup"
                old_value = snapshot(old_locator, query, user)
                active_label.name, active_label.phase = "new", "warmup"
                new_value = snapshot(new_locator, query, user)
                if old_value != new_value:
                    raise AssertionError("warmup full output/model_input mismatch")
                last = new_value
            active_label.name = None
            active_label.phase = None
            return worker_id, last

        with ThreadPoolExecutor(max_workers=case_spec["concurrency"]) as pool:
            list(pool.map(warm_worker, range(case_spec["concurrency"])))

        samples: list[dict[str, Any]] = []
        checkpoints: list[dict[str, Any]] = []
        round_count = case_spec["rounds"]
        for round_id in range(round_count):
            order = round_order(round_id)
            outputs: dict[str, dict[int, list[tuple[list[list[Any]], str, int]]]] = {}
            progress["in_progress"].update({
                "last_round_status": "round_started",
                "round_id": round_id,
                "phase_order": order,
                "completed_rounds": round_id,
                "samples": list(samples),
                "partial_phase_outputs": {},
            })
            write_json_atomic(output_path, progress)
            for implementation in order:
                locator_class = baseline_class if implementation == "old" else new_class
                outputs[implementation] = {}
                with ThreadPoolExecutor(max_workers=case_spec["concurrency"]) as pool:
                    futures = [pool.submit(
                        _timed_worker, worker_id, implementation, locator_class, db, query, user,
                        case_spec["samples_per_worker_per_round"], active_label,
                    ) for worker_id in range(case_spec["concurrency"])]
                    for future in as_completed(futures):
                        worker_id, worker_samples = future.result()
                        outputs[implementation][worker_id] = worker_samples
                        progress["in_progress"]["partial_phase_outputs"][implementation] = {
                            str(key): [
                                {"result": item[0], "model_input": item[1], "elapsed_ns": item[2]}
                                for item in value
                            ]
                            for key, value in outputs[implementation].items()
                        }
                        progress["in_progress"]["last_completed_implementation"] = implementation
                        write_json_atomic(output_path, progress)
            for worker_id in range(case_spec["concurrency"]):
                for sample_id in range(case_spec["samples_per_worker_per_round"]):
                    old_value = outputs["old"][worker_id][sample_id]
                    new_value = outputs["new"][worker_id][sample_id]
                    if old_value[:2] != new_value[:2]:
                        raise AssertionError("timed full output/model_input mismatch")
                    samples.append({
                        "round": round_id, "worker": worker_id, "sample": sample_id,
                        "phase_order": order, "old_ns": old_value[2], "new_ns": new_value[2],
                        "old_result": old_value[0], "new_result": new_value[0],
                        "old_model_input": old_value[1], "new_model_input": new_value[1],
                    })
            progress["in_progress"] = {
                **case_spec,
                "last_round_status": "round_complete_pending_resource_gate",
                "completed_rounds": round_id + 1,
                "samples": list(samples),
                "repository_counters": repository_counter_snapshot(store),
                **_legacy_counter_fields(repository_counter_snapshot(store)),
                "database_counters": database_counter_snapshot(store),
                "actual_sql": store["sql"],
                "resource_checkpoints": list(checkpoints),
            }
            write_json_atomic(output_path, progress)
            resource = enforce_resource_gate(sample_resources(), **thresholds)
            checkpoints.append(resource)
            progress["in_progress"]["last_round_status"] = "round_resource_gate_passed"
            progress["in_progress"]["resource_checkpoints"] = list(checkpoints)
            write_json_atomic(output_path, progress)

        after_hash = logical_database_hash(db)
        progress["in_progress"]["db_hash_after"] = after_hash
        progress["in_progress"]["last_round_status"] = "all_rounds_complete_hash_checked"
        write_json_atomic(output_path, progress)
        if before_hash != after_hash:
            raise AssertionError("fixture full-table logical hash changed")
        counter_snapshot = repository_counter_snapshot(store)
        return {
            **case_spec,
            "db_hash_before": before_hash["sha256"],
            "db_hash_after": after_hash["sha256"],
            "db_hash_details_before": before_hash,
            "db_hash_details_after": after_hash,
            "fixture_summary": fixture_summary,
            "diagnostic_probe": diagnostics,
            "semantic_probe": semantic,
            "repository_counters": counter_snapshot,
            **_legacy_counter_fields(counter_snapshot),
            "database_counters": database_counter_snapshot(store),
            "actual_sql": store["sql"],
            "sample_count": len(samples),
            "samples": samples,
            "old_median_ns": statistics.median(sample["old_ns"] for sample in samples),
            "new_median_ns": statistics.median(sample["new_ns"] for sample in samples),
            "old_max_ns": max(sample["old_ns"] for sample in samples),
            "new_max_ns": max(sample["new_ns"] for sample in samples),
            "resource_checkpoints": checkpoints,
            "row_count_scope": "Returned list lengths from the four wrapped repository methods only; excludes other SQLite cursor/query rows and is not a total-load count.",
        }
    finally:
        db.close()


def _json_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze", help="write a complete clean-source manifest")
    freeze.add_argument("--manifest", required=True, type=Path)
    check = subparsers.add_parser("check", help="verify a frozen source manifest without RAG/DB imports")
    check.add_argument("--manifest", required=True, type=Path)
    check.add_argument("--output", required=True, type=Path)
    run = subparsers.add_parser("run", help="run a synthetic paired acceptance matrix")
    run.add_argument("--manifest", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--min-free-bytes", required=True, type=int)
    run.add_argument("--max-working-set-bytes", required=True, type=int)
    run.add_argument("--counts", required=True, help="comma-separated requested document counts")
    run.add_argument("--workers", required=True, help="comma-separated values 1,2")
    run.add_argument("--rounds", type=int, default=3)
    run.add_argument("--warmups", type=int, default=3)
    run.add_argument("--samples", type=int, default=10)
    run.add_argument("--acl-extension", action="store_true", help="add separate N100 ACL extension cases")
    return parser


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _write_preflight_start(output: Path, command: str) -> dict[str, Any]:
    progress = {
        "schema_version": 1,
        "status": "starting",
        "command": command,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pid": os.getpid(),
        "platform": platform.platform(),
        "python": sys.version,
        "output": str(output),
        "runs": [],
    }
    write_json_new(output, progress)
    return progress


def _run_matrix(args: argparse.Namespace, output: Path, progress: dict[str, Any]) -> int:
    run_root = output.parent / f"{output.stem}-fixtures"
    if run_root.exists():
        raise AcceptanceError("fixture output directory already exists", code="run_directory_exists", details={"path": str(run_root)})
    run_root.mkdir(parents=True, exist_ok=False)
    rag_context = None
    try:
        progress["network_guard"] = install_offline_network_guard()
        progress["environment"] = sanitize_process_environment(run_root)
        progress["priority"] = set_process_priority()
        progress["sqlite_version"] = sqlite3.sqlite_version
        thresholds = {"min_free_bytes": args.min_free_bytes, "max_working_set_bytes": args.max_working_set_bytes}
        progress["resource_initial"] = enforce_resource_gate(sample_resources(), **thresholds)
        sqlite_audit = install_sqlite_path_audit(run_root)
        progress["sqlite_audit"] = sqlite_audit
        rag_context, isolated_paths = configure_isolated_context(run_root)
        progress["isolated_paths"] = isolated_paths
        progress["environment"]["environment_allowlist"] = sorted(os.environ)
        progress["status"] = "preflight"
        write_json_atomic(output, progress)

        from rag.materials.locator import MaterialLocator

        baseline_class = load_frozen_baseline()
        counts = tuple(int(value) for value in args.counts.split(","))
        workers = tuple(int(value) for value in args.workers.split(","))
        if not counts or any(value < 1 for value in counts) or not workers or any(value not in (1, 2) for value in workers):
            raise AcceptanceError("invalid matrix counts/workers", code="matrix_parameters_invalid")
        if (counts != CORE_COUNTS or workers != CORE_WORKERS or args.rounds != 3 or args.warmups != 3 or args.samples != 10
                or args.min_free_bytes <= 0 or args.max_working_set_bytes <= 0):
            raise AcceptanceError(
                "formal run must use the complete frozen core matrix and explicit positive resource thresholds",
                code="matrix_parameters_invalid",
                details={"required_counts": list(CORE_COUNTS), "required_workers": list(CORE_WORKERS), "rounds": 3, "warmups": 3, "samples": 10},
            )
        cases = core_case_matrix(counts, workers, rounds=args.rounds, warmups=args.warmups, samples=args.samples)
        if args.acl_extension:
            cases.extend(acl_extension_matrix(workers, rounds=args.rounds, warmups=args.warmups, samples=args.samples))
        progress["matrix"] = {"core": [case for case in cases if case["matrix_group"] == "core"], "acl_extension": [case for case in cases if case["matrix_group"] == "acl_extension"]}
        progress["status"] = "running"
        write_json_atomic(output, progress)
    except BaseException:
        if rag_context is not None:
            rag_context.user_db.close()
        raise
    try:
        for case_spec in cases:
            case_dir = run_root / case_spec["directory"]
            record = run_case(case_spec=case_spec, case_dir=case_dir, baseline_class=baseline_class, new_class=MaterialLocator, progress=progress, output_path=output, thresholds=thresholds)
            progress["runs"].append(record)
            progress.pop("in_progress", None)
            write_json_atomic(output, progress)
        progress["resource_final"] = enforce_resource_gate(sample_resources(), **thresholds)
        progress["source_freeze_final"] = verify_source_freeze(ROOT, args.manifest)
        progress["status"] = "complete"
        write_json_atomic(output, progress)
        return 0
    except ResourceGateError as exc:
        progress["status"] = "resource_blocked_with_partial" if progress.get("runs") or progress.get("in_progress") else "resource_blocked"
        progress["error"] = {"code": exc.details.get("error_code", "resource_gate_failed"), "message": str(exc), "details": exc.details}
        write_json_atomic(output, progress)
        return 2
    except BaseException as exc:
        progress["status"] = "failed_with_partial" if progress.get("runs") or progress.get("in_progress") else "failed"
        progress["error"] = {"code": "run_failed", "type": type(exc).__name__, "message": str(exc)}
        write_json_atomic(output, progress)
        return 2
    finally:
        if rag_context is not None:
            rag_context.user_db.close()


def main(argv: list[str] | None = None) -> int:
    try:
        args = _json_parser().parse_args(argv)
        if args.command == "freeze":
            manifest = validate_output_path(args.manifest, must_not_exist=True)
            payload = create_source_freeze(ROOT)
            write_json_new(manifest, payload)
            _emit({"status": "frozen", "manifest": str(manifest), "commit": payload["commit"], "files": len(payload["files"])})
            return 0

        output = validate_output_path(args.output, must_not_exist=True)
        progress = _write_preflight_start(output, args.command)
        try:
            if args.command == "check":
                progress["status"] = "checking_freeze"
                write_json_atomic(output, progress)
                progress["source_freeze"] = verify_source_freeze(ROOT, args.manifest)
                progress["status"] = "checked"
                write_json_atomic(output, progress)
                _emit({"status": "checked", "output": str(output), "source_freeze": progress["source_freeze"]})
                return 0
            progress["source_freeze"] = verify_source_freeze(ROOT, args.manifest)
            baseline_hash = source_hashes(BASELINE)
            new_locator_hash = source_hashes(ROOT / "rag" / "materials" / "locator.py")
            progress["source_sha256"] = {"baseline_raw": baseline_hash["raw_sha256"], "baseline": baseline_hash["lf_sha256"], "new": new_locator_hash["lf_sha256"]}
            progress["baseline_sha256"] = baseline_hash["lf_sha256"]
            progress["new_sha256"] = new_locator_hash["lf_sha256"]
            write_json_atomic(output, progress)
            result = _run_matrix(args, output, progress)
            _emit({"status": progress["status"], "output": str(output), "runs": len(progress["runs"])})
            return result
        except BaseException as exc:
            progress["status"] = "resource_blocked" if isinstance(exc, ResourceGateError) else "failed"
            details = getattr(exc, "details", {})
            code = details.get("error_code", getattr(exc, "code", "preflight_failed"))
            progress["error"] = {"code": code, "type": type(exc).__name__, "message": str(exc), "details": details}
            write_json_atomic(output, progress)
            _emit({"status": "failed", "error": progress["error"], "output": str(output)})
            return 2
    except (AcceptanceError, OSError, ValueError) as exc:
        payload = {"status": "failed", "error": {"code": getattr(exc, "code", "invalid_arguments"), "message": str(exc), "details": getattr(exc, "details", {})}}
        _emit(payload)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
