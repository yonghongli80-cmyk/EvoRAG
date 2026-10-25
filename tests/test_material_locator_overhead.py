"""Allocation and compatibility contracts for the material locator."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import rag.materials.locator as locator_module
from rag.materials.locator import MaterialLocator
from rag.materials.models import CitationLocation, MaterialLocationQuery
from rag.user_db import UserDB


def test_default_locator_consumes_visibility_iterator_without_full_mapping(tmp_path, monkeypatch):
    db = UserDB(str(tmp_path / "locator.db"))
    db.create_material_schema()
    admin_id = db.create_user("locator-admin", "Password-123!")
    db.set_user_admin(admin_id, True)
    document_ids = []
    for filename in ("first.pdf", "second.pdf"):
        document_id = db.create_document_permission(filename, "kb-1", admin_id)
        version_id = db.create_document_version(document_id, filename, f"/tmp/{filename}", "hash", admin_id)
        db.activate_document_version(document_id, version_id, "kb-1", filename)
        document_ids.append(document_id)
    user = db.get_user_by_id(admin_id)
    iterator_calls = []
    original_iterator = locator_module.iter_visible_material_documents

    def tracked_iterator(db, current_user):
        iterator_calls.append((db, current_user))
        yield from original_iterator(db, current_user)

    def forbidden_mapping(*args, **kwargs):
        raise AssertionError("default locator copied the visible-documents mapping")

    monkeypatch.setattr(locator_module, "iter_visible_material_documents", tracked_iterator)
    # A module global shadows only the built-in constructor. Dict literals used
    # by the legacy callback are unaffected, so this isolates the locator copy.
    monkeypatch.setattr(locator_module, "dict", forbidden_mapping, raising=False)

    semantic_calls = []
    fts_calls = []

    def semantic(text, allowed_ids, top_k):
        semantic_calls.append((text, set(allowed_ids), top_k))
        return []

    original_fts = db.search_material_search_fts

    def tracked_fts(text, top_k):
        fts_calls.append((text, top_k))
        return original_fts(text, top_k)

    db.search_material_search_fts = tracked_fts
    locator = MaterialLocator(db, semantic_retriever=semantic)
    results = locator.locate(MaterialLocationQuery(text="合同正文", top_k=1), user)

    assert iterator_calls == [(db, user)]
    assert results == []
    assert locator.model_input == "first.pdf  \nsecond.pdf  "
    assert fts_calls == [("合同正文", 1)]
    assert semantic_calls == [("合同正文", {1, 2}, 1)]
    db.close()


def test_custom_callback_snapshot_and_fallback_precede_filter(monkeypatch):
    rows = {
        1: {
            "id": 1,
            "current_version_id": 101,
            "doc_name": "first.pdf",
            "kb_id": "kb",
            "subject_user_id": 7,
            "document_type": "tax",
            "logical_path": "People/Ångström",
            "updated_at": None,
        },
        2: {
            "id": 2,
            "current_version_id": 102,
            "doc_name": "second.pdf",
            "kb_id": "kb",
            "subject_user_id": 8,
            "document_type": "other",
            "logical_path": "people/angstrom",
            "updated_at": 100,
        },
    }
    original_versions = {
        1: {"id": 101, "original_filename": "first-v1.pdf", "validity_status": "reported_outdated"},
        2: {"id": 102, "original_filename": "second-v1.pdf", "validity_status": None},
    }

    class FakeDB:
        def __init__(self):
            self.version_calls = []
            self.fts_calls = []

        def get_current_document_version(self, document_id):
            self.version_calls.append(document_id)
            return original_versions[document_id]

        def search_material_search_fts(self, text, top_k):
            self.fts_calls.append((text, top_k))
            return [{"document_id": 2}]

    db = FakeDB()
    semantic_calls = []

    def semantic(text, allowed_ids, top_k):
        semantic_calls.append((text, set(allowed_ids), top_k))
        return [SimpleNamespace(document_id=1, citation=CitationLocation(kind="metadata", value="s"))]

    def visible_docs(_db, _user):
        return rows

    locator = MaterialLocator(db, semantic_retriever=semantic, visible_docs=visible_docs)
    query = MaterialLocationQuery(
        text="tax",
        subject=7,
        document_types=("tax",),
        uploaded_after=datetime.fromtimestamp(0, UTC),
        logical_path_prefix="People/Å",
        top_k=1,
    )

    results = locator.locate(query, {"id": 7, "is_admin": False})

    assert db.version_calls == [1, 2]
    assert db.fts_calls == [("tax", 1)]
    assert semantic_calls == [("tax", {101}, 1)]
    assert locator.model_input == "first.pdf People/Ångström tax"
    assert [(result.document_id, result.document_version_id, result.filename) for result in results] == [
        (1, 101, "first-v1.pdf")
    ]
    assert results[0].ocr_warning is True


def test_custom_callback_failure_is_not_hidden_by_filtering():
    class FakeDB:
        def get_current_document_version(self, document_id):
            raise RuntimeError("version lookup failed")

    row = {
        1: {
            "id": 1,
            "current_version_id": 101,
            "doc_name": "excluded.pdf",
            "kb_id": "kb",
            "subject_user_id": 9,
            "document_type": "other",
            "logical_path": "Elsewhere",
        }
    }
    locator = MaterialLocator(FakeDB(), visible_docs=lambda _db, _user: row)

    with pytest.raises(RuntimeError, match="version lookup failed"):
        locator.locate(
            MaterialLocationQuery(text="query", subject=7, document_types=("tax",)),
            {"id": 7},
        )


def test_module_patch_and_instance_override_remain_custom_callbacks(monkeypatch):
    row = {
        3: {
            "id": 3,
            "current_version_id": 103,
            "current_filename": "module.pdf",
            "current_validity_status": "reported_outdated",
            "doc_name": "module.pdf",
            "logical_path": "Records/Å",
            "document_type": "tax",
            "subject_user_id": 7,
            "updated_at": None,
        }
    }

    class FakeDB:
        def get_current_document_version(self, _document_id):
            raise AssertionError("complete custom rows must not fall back to a lookup")

        def search_material_search_fts(self, _text, _top_k):
            return []

    calls = []

    def module_callback(_db, _user):
        calls.append("module")
        return row

    monkeypatch.setattr(locator_module, "_visible_documents", module_callback)
    module_locator = MaterialLocator(FakeDB())
    module_results = module_locator.locate(MaterialLocationQuery(text="query", subject="self"), {"id": 7})
    assert calls == ["module"]
    assert module_locator.model_input == "module.pdf Records/Å tax"
    assert [(result.document_id, result.filename, result.ocr_warning) for result in module_results] == [
        (3, "module.pdf", True)
    ]

    instance_locator = MaterialLocator(FakeDB())
    instance_locator._visible_docs = lambda _db, _user: row
    instance_results = instance_locator.locate(MaterialLocationQuery(text="query", subject=0), {"id": 7})
    assert instance_results == []
    assert instance_locator.model_input == ""
