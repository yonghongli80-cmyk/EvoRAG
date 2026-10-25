"""Full ordered parity checks against the shipped 542a36f locator source."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rag.materials.locator import MaterialLocator
from rag.materials.models import CitationLocation, MaterialLocationQuery
from rag.user_db import UserDB

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "tests/fixtures/material_locator_542a36f.py"
GOLDEN_PATH = ROOT / "tests/fixtures/material_locator_expected.json"
BASELINE_SHA256 = "54593297c81a31000dee9c26336db3198bc85e3e334f001cddb7f33766434bfc"


def _baseline_module():
    # Git's core.autocrlf may materialize the frozen LF blob as CRLF on Windows.
    normalized_source = BASELINE_PATH.read_text(encoding="utf-8").encode("utf-8")
    actual = hashlib.sha256(normalized_source).hexdigest()
    assert actual == BASELINE_SHA256, "frozen compatibility source changed"
    spec = importlib.util.spec_from_file_location("material_locator_542a36f", BASELINE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _serialize(results):
    return [
        {
            "document_id": row.document_id,
            "document_version_id": row.document_version_id,
            "filename": row.filename,
            "logical_path": row.logical_path,
            "document_type": row.document_type,
            "updated_at": row.updated_at,
            "ocr_warning": row.ocr_warning,
            "citation.location.kind": row.citation.location.kind,
            "citation.location.value": row.citation.location.value,
            "rank": row.rank,
            "source": row.source,
        }
        for row in results
    ]


def _frozen_golden_cases(db, people):
    old_mod = _baseline_module()
    cases = {}
    scenarios = {
        name: (name, MaterialLocationQuery(text="searchable", subject="self", top_k=2))
        for name in ("alice", "bob", "admin")
    }
    scenarios.update(
        {
            "subject_zero_positive": ("alice", MaterialLocationQuery(text="searchable", subject=0, top_k=1)),
            "subject_zero_negative": ("alice", MaterialLocationQuery(text="searchable", subject=999999, top_k=1)),
            "path_case_positive": ("alice", MaterialLocationQuery(text="searchable", logical_path_prefix="HR/", top_k=2)),
            "path_case_negative": ("alice", MaterialLocationQuery(text="searchable", logical_path_prefix="hr/", top_k=2)),
            "path_unicode_positive": ("alice", MaterialLocationQuery(text="searchable", logical_path_prefix="资料/", top_k=2)),
            "path_unicode_negative": ("alice", MaterialLocationQuery(text="searchable", logical_path_prefix="資料/", top_k=2)),
        }
    )
    for case_name, (name, query) in scenarios.items():
        trace = TraceDB(db)
        locator = old_mod.MaterialLocator(trace, semantic_retriever=trace.retrieve)
        user = {"id": people[name], "username": name, "is_admin": name == "admin"}
        cases[case_name] = {
            "result": _serialize(locator.locate(query, user)),
            "model_input": locator.model_input,
            "semantic_calls": [[text, sorted(allowed), top_k]
                               for text, allowed, top_k in trace.semantic_calls],
            "fts_calls": [list(call) for call in trace.fts_calls],
        }
    return cases


class TraceDB:
    """Record retrieval arguments while delegating real permission and SQL work."""

    def __init__(self, db, semantic_hits=()):
        self._db = db
        self.semantic_hits = tuple(semantic_hits)
        self.semantic_calls = []
        self.fts_calls = []

    def __getattr__(self, name):
        return getattr(self._db, name)

    def search_material_search_fts(self, text, limit=50):
        self.fts_calls.append((text, limit))
        return self._db.search_material_search_fts(text, limit)

    def retrieve(self, text, allowed_version_ids, top_k):
        self.semantic_calls.append((text, frozenset(allowed_version_ids), top_k))
        return list(self.semantic_hits)[:top_k]


@pytest.fixture
def comparison_db(tmp_path):
    db = UserDB(str(tmp_path / "material-locator-equivalence.sqlite3"))
    db.create_material_schema()
    alice_id = db.create_user("equiv-alice", "Password-123!")
    bob_id = db.create_user("equiv-bob", "Password-123!")
    admin_id = db.create_user("equiv-admin", "Password-123!")
    people = {"alice": alice_id, "bob": bob_id, "admin": admin_id}

    def seed(name, owner="alice", kb="legacy", *, scope="private", subject=None,
             doc_type="memo", path="HR/Shared", current=True, deleted=False,
             updated_at=1_750_000_000.0, text="shared searchable evidence", share=()):
        owner_id = people[owner]
        doc_id = db.create_document_permission(name, kb, owner_id, scope=scope)
        with db.locked_transaction() as conn:
            conn.execute(
                "UPDATE document_permissions SET subject_user_id=?, document_type=?, logical_path=?, updated_at=? WHERE id=?",
                (people[subject] if isinstance(subject, str) else subject, doc_type, path, updated_at, doc_id),
            )
            if deleted:
                conn.execute("UPDATE document_permissions SET deleted_at=1 WHERE id=?", (doc_id,))
        for user in share:
            db.share_document(doc_id, people[user], owner_id, "edit" if user == "bob" else "view")
        version_id = None
        if current:
            version_id = db.create_document_version(
                doc_id, name, f"/synthetic/{doc_id}/{name}", f"hash-{doc_id}", owner_id
            )
            db.activate_document_version(doc_id, version_id, kb, name)
            db.insert_material_search_fts(doc_id, version_id, name, path, doc_type, "[]", text)
        # Activation updates the permission timestamp; reset it afterward so golden snapshots
        # preserve and compare the real updated_at value deterministically.
        with db.locked_transaction() as conn:
            conn.execute("UPDATE document_permissions SET updated_at=? WHERE id=?", (updated_at, doc_id))
        return doc_id, version_id

    docs = []
    docs.append(seed("dup.txt", "alice", scope="public", subject="alice"))
    docs.append(seed("dup.txt", "bob", "legacy-alt", scope="private", subject="bob"))
    docs.append(seed("owner.txt", "alice", subject="alice"))
    docs.append(seed("shared-view.txt", "bob", scope="shared", share=("alice",)))
    docs.append(seed("shared-edit.txt", "bob", scope="shared", share=("alice",)))
    docs.append(seed("private.txt", "bob", subject="bob"))
    docs.append(seed("old.txt", "alice", current=False))
    docs.append(seed("deleted.txt", "alice", deleted=True))
    docs.append(seed("zero-subject.txt", "alice", subject=0, path="HR/Zero"))
    docs.append(seed("unicode-path.txt", "alice", subject="alice", path="资料/入职材料"))
    # Organization policy is evaluated by the real locator authorization service.
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,parent_code,name,kind) VALUES('EQ',NULL,'Equiv','team')")
        conn.execute("INSERT INTO org_people(person_code,user_id,display_name,job_title,active) VALUES('EQ-A',?,'Alice','Analyst',1)", (alice_id,))
        conn.execute("INSERT INTO org_memberships(person_code,org_code,is_primary) VALUES('EQ-A','EQ',1)")
        conn.execute("INSERT INTO kb_metadata(kb_id,name,owner_id,scope) VALUES('org-eq','Org EQ',?, 'private')", (alice_id,))
        conn.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('org-eq','EQ','organization')")
    docs.append(seed("org-visible.txt", "bob", "org-eq", subject="alice"))
    docs.append(seed("org-hidden.txt", "bob", "org-eq", subject="bob"))
    yield db, people, docs
    db.close()


def _compare(db, people, user_name, query):
    old_mod = _baseline_module()
    # Both readers use actual authorization services and the same SQLite DB.
    old_trace, new_trace = TraceDB(db), TraceDB(db)
    old = old_mod.MaterialLocator(old_trace, semantic_retriever=old_trace.retrieve)
    new = MaterialLocator(new_trace, semantic_retriever=new_trace.retrieve)
    user = {"id": people[user_name], "username": user_name, "is_admin": user_name == "admin"}
    old_rows = old.locate(query, user)
    new_rows = new.locate(query, user)
    assert _serialize(new_rows) == _serialize(old_rows)
    assert new.model_input == old.model_input
    assert new_trace.semantic_calls == old_trace.semantic_calls
    assert new_trace.fts_calls == old_trace.fts_calls
    return old_rows, old.model_input, old_trace.semantic_calls, old_trace.fts_calls


def test_golden_tracks_frozen_source_and_full_comparison_contract(comparison_db):
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert golden["baseline_sha256"] == BASELINE_SHA256
    source = BASELINE_PATH.read_text(encoding="utf-8").encode("utf-8")
    assert hashlib.sha256(source).hexdigest() == BASELINE_SHA256
    assert "model_input_exact_string" in golden["comparison"]["also_compare"]
    assert "unauthorized_names_absent_from_results_and_model_input" in golden["comparison"]["also_compare"]
    db, people, _ = comparison_db
    expected_cases = _frozen_golden_cases(db, people)
    if os.environ.get("TASK3_REGENERATE_GOLDEN") == "1":
        golden["expected_cases"] = expected_cases
        GOLDEN_PATH.write_text(json.dumps(golden, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        actual_cases = golden.get("expected_cases")
        mismatches = {
            name: {"golden": actual_cases.get(name), "current_frozen_source": value}
            for name, value in expected_cases.items()
            if actual_cases is None or actual_cases.get(name) != value
        }
        assert not mismatches, json.dumps(mismatches, ensure_ascii=False, indent=2)


@pytest.mark.parametrize("user_name", ["alice", "bob", "admin"])
def test_legacy_and_org_visibility_keep_complete_outputs_and_diagnostics(comparison_db, user_name):
    db, people, _ = comparison_db
    results, model_input, _, _ = _compare(
        db, people, user_name, MaterialLocationQuery(text="searchable", subject="self", top_k=2)
    )
    denied_names = {
        "alice": ("private.txt", "org-hidden.txt"),
        "bob": ("owner.txt", "org-visible.txt", "org-hidden.txt"),
        # Organization policy is checked before the legacy-admin branch.
        "admin": ("org-visible.txt", "org-hidden.txt"),
    }[user_name]
    for name in denied_names:
        assert name not in model_input
        assert all(result.filename != name for result in results)


@pytest.mark.parametrize(
    "query",
    [
        MaterialLocationQuery(text="searchable", subject=0, top_k=1),
        MaterialLocationQuery(text="searchable", subject=999999, top_k=1),
        MaterialLocationQuery(text="searchable", document_types=("memo",), top_k=2),
        MaterialLocationQuery(text="searchable", logical_path_prefix="HR/", top_k=1),
        MaterialLocationQuery(text="searchable", logical_path_prefix="hr/", top_k=4),
        MaterialLocationQuery(text="searchable", logical_path_prefix="资料/", top_k=1),
        MaterialLocationQuery(text="searchable", logical_path_prefix="資料/", top_k=4),
        MaterialLocationQuery(text="searchable", uploaded_after="2025-06-15T15:06:40Z", top_k=2),
    ],
)
def test_filters_and_topk_do_not_change_allowed_semantic_set(comparison_db, query):
    db, people, _ = comparison_db
    results, model_input, semantic_calls, fts_calls = _compare(db, people, "alice", query)
    if query.subject == 0:
        assert any(row.filename == "zero-subject.txt" for row in results)
    if query.subject == 999999:
        assert not results and not model_input
    if query.logical_path_prefix == "HR/":
        assert model_input and "owner.txt" in model_input
    if query.logical_path_prefix == "hr/":
        assert not model_input
    if query.logical_path_prefix == "资料/":
        assert "unicode-path.txt" in model_input
    if query.logical_path_prefix == "資料/":
        assert not model_input
    if model_input:
        assert semantic_calls
        assert fts_calls == [(query.text, query.top_k)]
    else:
        assert semantic_calls == []
        assert fts_calls == []
    assert "private.txt" not in model_input


def test_combined_filters_match_timestamp_and_prefix_python_semantics(comparison_db):
    db, people, _ = comparison_db
    query = MaterialLocationQuery(
        text="searchable", subject=people["alice"], document_types=("memo",),
        uploaded_after="2025-06-15T15:06:40Z", logical_path_prefix="HR/", top_k=1,
    )
    _, model_input, semantic_calls, fts_calls = _compare(db, people, "alice", query)
    assert model_input
    assert semantic_calls and len(semantic_calls[0][1]) > query.top_k
    assert fts_calls == [(query.text, query.top_k)]


def test_semantic_overlap_keeps_source_order_and_complete_allowed_ids(comparison_db):
    db, people, seeded = comparison_db
    user = {"id": people["alice"], "username": "alice", "is_admin": False}
    old_mod = _baseline_module()
    visible = old_mod._visible_documents(db, user)
    # Return hits spanning more visible IDs than top_k; both locators must pass the full set.
    semantic_hits = [
        SimpleNamespace(document_id=doc_id, citation=CitationLocation(kind="page", value=str(i)))
        for i, doc_id in enumerate(visible)
    ]
    old_trace, new_trace = TraceDB(db, semantic_hits), TraceDB(db, semantic_hits)
    old = old_mod.MaterialLocator(old_trace, semantic_retriever=old_trace.retrieve)
    new = MaterialLocator(new_trace, semantic_retriever=new_trace.retrieve)
    query = MaterialLocationQuery(text="searchable", subject="self", top_k=1)
    assert _serialize(new.locate(query, user)) == _serialize(old.locate(query, user))
    assert new.model_input == old.model_input
    assert new_trace.semantic_calls == old_trace.semantic_calls
    assert len(old_trace.semantic_calls[0][1]) > query.top_k


def test_custom_callback_snapshot_uses_real_visible_rows_and_version_fallback(comparison_db):
    db, people, _ = comparison_db
    old_mod = _baseline_module()
    user = {"id": people["alice"], "username": "alice", "is_admin": False}
    rows = old_mod._visible_documents(db, user)
    # Simulate a supported extension callback row that predates joined-current fields.
    custom_rows = {
        doc_id: {key: value for key, value in row.items()
                 if key not in {"current_filename", "current_validity_status", "current_version_no"}}
        for doc_id, row in rows.items()
    }
    def callback(_db, _user):
        return custom_rows
    query = MaterialLocationQuery(text="searchable", subject="self", top_k=3)
    old_trace, new_trace = TraceDB(db), TraceDB(db)
    old = old_mod.MaterialLocator(old_trace, semantic_retriever=old_trace.retrieve, visible_docs=callback)
    new = MaterialLocator(new_trace, semantic_retriever=new_trace.retrieve, visible_docs=callback)
    assert _serialize(new.locate(query, user)) == _serialize(old.locate(query, user))
    assert new.model_input == old.model_input
    assert new_trace.semantic_calls == old_trace.semantic_calls
    assert new_trace.fts_calls == old_trace.fts_calls


def test_post_construction_visible_callback_override_is_not_bypassed(comparison_db):
    db, people, _ = comparison_db
    old_mod = _baseline_module()
    user = {"id": people["alice"], "username": "alice", "is_admin": False}
    rows = old_mod._visible_documents(db, user)
    old_trace, new_trace = TraceDB(db), TraceDB(db)
    old = old_mod.MaterialLocator(old_trace, semantic_retriever=old_trace.retrieve)
    new = MaterialLocator(new_trace, semantic_retriever=new_trace.retrieve)
    old._visible_docs = lambda _db, _user: rows
    new._visible_docs = lambda _db, _user: rows
    query = MaterialLocationQuery(text="searchable", subject="self", top_k=3)
    assert _serialize(new.locate(query, user)) == _serialize(old.locate(query, user))
    assert new.model_input == old.model_input
    assert new_trace.semantic_calls == old_trace.semantic_calls
    assert new_trace.fts_calls == old_trace.fts_calls


def test_module_callback_patch_is_seen_by_new_default_constructor(comparison_db, monkeypatch):
    import rag.materials.locator as locator_module

    db, people, _ = comparison_db
    old_mod = _baseline_module()
    user = {"id": people["alice"], "username": "alice", "is_admin": False}
    rows = old_mod._visible_documents(db, user)
    def callback(_db, _user):
        return rows
    monkeypatch.setattr(locator_module, "_visible_documents", callback)
    trace = TraceDB(db)
    locator = MaterialLocator(trace, semantic_retriever=trace.retrieve)
    assert locator._visible_docs is callback
    query = MaterialLocationQuery(text="searchable", subject="self", top_k=3)
    # The patched callback still reaches the locator's normal retrieval and diagnostics path.
    locator.locate(query, user)
    assert locator.model_input
    assert trace.semantic_calls and trace.fts_calls == [(query.text, query.top_k)]
