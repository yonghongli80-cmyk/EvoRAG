from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.material_locator_acceptance_support import (
    AcceptanceError,
    create_source_freeze,
    install_offline_network_guard,
    install_repository_counters,
    install_sqlite_path_audit,
    new_counter_store,
    repository_counter_snapshot,
    sanitize_process_environment,
    source_hashes,
    verify_source_freeze,
)
from scripts.paired_material_locator_acceptance import (
    acl_extension_matrix,
    case_directory_name,
    core_case_matrix,
    main,
    round_order,
)


def test_core_matrix_has_18_unique_paths_and_810_pairs():
    cases = core_case_matrix()

    assert len(cases) == 18
    paths = [
        case_directory_name(
            requested_n=case["requested_n"],
            actual_n=case["actual_n"],
            case=case["case"],
            concurrency=case["concurrency"],
        )
        for case in cases
    ]
    assert len(set(paths)) == 18
    assert sum(case["expected_pairs"] for case in cases) == 810
    small = [case for case in cases if case["case"] in {"many-kb-user", "two-kb-admin"}]
    assert {case["actual_n"] for case in small} == {100}
    assert len({case["requested_n"] for case in small}) == 3


def test_acl_extension_matrix_is_separate_two_cases_and_90_pairs():
    cases = acl_extension_matrix()
    assert len(cases) == 2
    assert {case["concurrency"] for case in cases} == {1, 2}
    assert all(case["actual_n"] == 100 for case in cases)
    assert sum(case["expected_pairs"] for case in cases) == 90


def test_round_order_alternates_whole_implementations():
    assert round_order(0) == ["old", "new"]
    assert round_order(1) == ["new", "old"]
    assert round_order(2) == ["old", "new"]


def test_source_hashes_record_raw_and_lf_normalized_text(tmp_path):
    path = tmp_path / "source.py"
    path.write_bytes(b"one\r\ntwo\r\n")
    hashes = source_hashes(path)
    import hashlib
    assert hashes["raw_sha256"] == hashlib.sha256(b"one\r\ntwo\r\n").hexdigest()
    assert hashes["lf_sha256"] == hashlib.sha256(b"one\ntwo\n").hexdigest()


def test_environment_sanitizer_drops_inherited_secrets_and_uses_private_paths(tmp_path):
    before = dict(os.environ)
    try:
        os.environ["MATERIAL_ACCEPTANCE_TEST_SECRET"] = "must-not-survive"
        os.environ["RAG_USERS_DB_PATH"] = "outside/users.sqlite3"
        summary = sanitize_process_environment(tmp_path / "run")
        assert summary["secrets_retained"] is False
        assert "MATERIAL_ACCEPTANCE_TEST_SECRET" not in os.environ
        assert "RAG_USERS_DB_PATH" not in os.environ
        assert {"HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"} <= set(os.environ)
        assert {"TEMP", "TMP", "TMPDIR"} <= set(os.environ)
        assert all(os.environ[name] == "1" for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"))
    finally:
        os.environ.clear()
        os.environ.update(before)


def test_network_guard_blocks_loopback_dns_and_connection_apis():
    originals = {
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "sendto": socket.socket.sendto,
        "create_connection": socket.create_connection,
        "getaddrinfo": socket.getaddrinfo,
        "gethostbyname": socket.gethostbyname,
        "gethostbyname_ex": socket.gethostbyname_ex,
        "gethostbyaddr": socket.gethostbyaddr,
        "getnameinfo": socket.getnameinfo,
        "sendmsg": getattr(socket.socket, "sendmsg", None),
    }
    try:
        guard = install_offline_network_guard()
        assert guard["installed"] is True
        with pytest.raises(OSError):
            socket.socket.connect(object(), ("127.0.0.1", 8080))
        with pytest.raises(OSError):
            socket.socket.connect_ex(object(), ("127.0.0.1", 8080))
        with pytest.raises(OSError):
            socket.socket.sendto(object(), b"blocked", ("127.0.0.1", 53))
        if originals["sendmsg"] is not None:
            with pytest.raises(OSError):
                socket.socket.sendmsg(object(), [b"blocked"], [], ("127.0.0.1", 53))
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", 8080))
        with pytest.raises(OSError):
            socket.getaddrinfo("example.invalid", 443)
        with pytest.raises(OSError):
            socket.gethostbyname("example.invalid")
        with pytest.raises(OSError):
            socket.gethostbyaddr("192.0.2.1")
        with pytest.raises(OSError):
            socket.getnameinfo(("192.0.2.1", 443), 0)
    finally:
        socket.socket.connect = originals["connect"]
        socket.socket.connect_ex = originals["connect_ex"]
        socket.socket.sendto = originals["sendto"]
        if originals["sendmsg"] is not None:
            socket.socket.sendmsg = originals["sendmsg"]
        socket.create_connection = originals["create_connection"]
        socket.getaddrinfo = originals["getaddrinfo"]
        socket.gethostbyname = originals["gethostbyname"]
        socket.gethostbyname_ex = originals["gethostbyname_ex"]
        socket.gethostbyaddr = originals["gethostbyaddr"]
        socket.getnameinfo = originals["getnameinfo"]
        if hasattr(socket, "_material_locator_guard_installed"):
            delattr(socket, "_material_locator_guard_installed")


def test_sqlite_path_audit_allows_only_run_root_and_rejects_uri(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    original_connect = sqlite3.connect
    original_dbapi_connect = sqlite3.dbapi2.connect
    audit = install_sqlite_path_audit(run_root)
    try:
        connection = sqlite3.connect(run_root / "allowed.sqlite3")
        connection.close()
        with pytest.raises(AcceptanceError) as outside:
            sqlite3.connect(tmp_path / "outside.sqlite3")
        assert outside.value.code == "sqlite_path_forbidden"
        with pytest.raises(AcceptanceError) as uri:
            sqlite3.connect("file:unsafe?mode=memory&cache=shared")
        assert uri.value.code == "sqlite_uri_forbidden"
        assert audit["opened_paths"] == [str((run_root / "allowed.sqlite3").resolve())]
    finally:
        sqlite3.connect = original_connect
        sqlite3.dbapi2.connect = original_dbapi_connect


def test_repository_counter_tracks_all_four_methods_and_each_phase():
    class Repository:
        def list_material_documents_with_current_version(self):
            return [{"id": 1}, {"id": 2}]

        def list_material_document_kb_ids(self):
            return ["kb-a"]

        def list_material_documents_for_kb(self, *_args, **_kwargs):
            return []

        def list_material_documents_for_access_plan(self, *_args, **_kwargs):
            return [{"id": 3}]

    from types import SimpleNamespace

    store = new_counter_store()
    active = SimpleNamespace(name="new", phase="diagnostic_probe")
    repo = Repository()
    install_repository_counters(repo, store, "new", active)
    repo.list_material_documents_with_current_version()
    repo.list_material_document_kb_ids()
    repo.list_material_documents_for_kb("kb-a", apply_legacy_acl=False)
    active.phase = "semantic_probe"
    repo.list_material_documents_for_access_plan([], user_id=None)
    counts = repository_counter_snapshot(store)

    assert counts["new"]["list_material_documents_with_current_version"]["diagnostic_probe"] == {"calls": 1, "returned_list_rows": 2}
    assert counts["new"]["list_material_document_kb_ids"]["diagnostic_probe"] == {"calls": 1, "returned_list_rows": 1}
    assert counts["new"]["list_material_documents_for_kb"]["diagnostic_probe"] == {"calls": 1, "returned_list_rows": 0}
    assert counts["new"]["list_material_documents_for_access_plan"]["semantic_probe"] == {"calls": 1, "returned_list_rows": 1}
    assert counts["old"]["list_material_documents_for_access_plan"]["semantic_probe"] == {"calls": 0, "returned_list_rows": 0}


def _temporary_freeze_repo(tmp_path, monkeypatch):
    import hashlib

    import scripts.material_locator_acceptance_support as support

    git = shutil.which("git")
    if git is None:
        import pytest
        pytest.skip("git is required only for the isolated temporary-repository fixture")
    root = tmp_path / "repo"
    files = {
        "rag/material.py": "source\n",
        "tests/test.py": "test\n",
        "scripts/tool.py": "tool\n",
        "config/settings.py": "setting\n",
        "config.py": "config\n",
        "pyproject.toml": "[project]\n",
        "tests/fixtures/material_locator_542a36f.py": "fixture\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    tools = []
    for index in range(5):
        relative = f"scripts/frozen_tool_{index}.py"
        (root / relative).write_text(f"tool-{index}\n", encoding="utf-8")
        tools.append(relative)
    subprocess.run([git, "-C", str(root), "init", "-q"], check=True, capture_output=True)
    subprocess.run([git, "-C", str(root), "config", "user.email", "fixture@example.invalid"], check=True, capture_output=True)
    subprocess.run([git, "-C", str(root), "config", "user.name", "Fixture"], check=True, capture_output=True)
    subprocess.run([git, "-C", str(root), "add", "--all"], check=True, capture_output=True)
    subprocess.run([git, "-C", str(root), "commit", "-qm", "fixture"], check=True, capture_output=True)
    monkeypatch.setattr(support, "SOURCE_DIRS", ("rag", "tests", "scripts", "config"))
    monkeypatch.setattr(support, "SOURCE_FILES", ("config.py", "pyproject.toml"))
    monkeypatch.setattr(support, "REQUIRED_SOURCE_FILES", ("rag/material.py", "tests/test.py", "pyproject.toml"))
    monkeypatch.setattr(support, "FROZEN_TOOL_FILES", tuple(tools))
    monkeypatch.setattr(support, "BASELINE_LF_SHA256", hashlib.sha256(b"fixture\n").hexdigest())
    return root, support


def test_freeze_and_check_bind_a_complete_clean_temporary_git_tree(tmp_path, monkeypatch):
    root, support = _temporary_freeze_repo(tmp_path, monkeypatch)
    manifest = create_source_freeze(root)
    path = root / "artifacts" / "acceptance-freeze.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")

    verified = verify_source_freeze(root, path)

    assert verified["verified"] is True
    assert verified["commit"] == manifest["commit"]
    assert verified["file_count"] >= 10


def test_freeze_check_rejects_dirty_and_untracked_source_tree(tmp_path, monkeypatch):
    root, _support = _temporary_freeze_repo(tmp_path, monkeypatch)
    manifest = create_source_freeze(root)
    path = root / "artifacts" / "acceptance-freeze.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    (root / "rag" / "new.py").write_text("untracked\n", encoding="utf-8")

    try:
        verify_source_freeze(root, path)
    except AcceptanceError as exc:
        assert exc.code == "source_tree_dirty"
    else:
        raise AssertionError("untracked source must invalidate the freeze")


def test_freeze_check_rejects_hash_manifest_tampering(tmp_path, monkeypatch):
    root, support = _temporary_freeze_repo(tmp_path, monkeypatch)
    manifest = create_source_freeze(root)
    manifest["files"][0]["raw_sha256"] = "0" * 64
    manifest["manifest_sha256"] = support._manifest_digest(manifest)
    path = root / "artifacts" / "acceptance-freeze.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")

    try:
        verify_source_freeze(root, path)
    except AcceptanceError as exc:
        assert exc.code == "freeze_hash_mismatch"
    else:
        raise AssertionError("tampered file hash must invalidate the freeze")


def test_check_rejects_missing_manifest_before_rag_import(tmp_path, capsys):
    output = Path(__file__).resolve().parents[1] / "artifacts" / "material-locator-acceptance" / f"test-preflight-{tmp_path.name}.json"
    before = {name for name in sys.modules if name == "rag" or name.startswith("rag.")}
    try:
        result = main(["check", "--manifest", str(tmp_path / "missing.json"), "--output", str(output)])
        after = {name for name in sys.modules if name == "rag" or name.startswith("rag.")}
        assert result != 0
        assert after == before
        assert output.exists()
        assert '"status"' in capsys.readouterr().out
    finally:
        output.unlink(missing_ok=True)


def test_check_refuses_to_overwrite_existing_output(tmp_path, capsys):
    output = Path(__file__).resolve().parents[1] / "artifacts" / "material-locator-acceptance" / f"test-preflight-{tmp_path.name}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("do-not-overwrite", encoding="utf-8")
    try:
        result = main(["check", "--manifest", str(tmp_path / "missing.json"), "--output", str(output)])
        assert result != 0
        assert output.read_text(encoding="utf-8") == "do-not-overwrite"
        assert "output" in capsys.readouterr().out.lower()
    finally:
        output.unlink(missing_ok=True)
