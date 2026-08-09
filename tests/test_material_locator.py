"""Task 8: ACL-first material location and authorized downloads."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from rag.materials.locator import MaterialLocator, material_result_response
from rag.materials.models import CitationLocation, MaterialLocationQuery
from rag.permissions import check_doc_permission
from rag.user_db import UserDB

PDF = b"%PDF-1.4\ncontract"


class FakeSemantic:
    """Deterministic semantic retriever that only returns allowed versions."""

    def __init__(self):
        self.hits = {}

    def add(self, version_id, document_id, text, citation):
        self.hits[version_id] = SimpleNamespace(
            document_id=document_id, version_id=version_id, text=text, citation=citation
        )

    def retrieve(self, query, allowed_version_ids=None, top_k=10):
        if allowed_version_ids is None:
            allowed_version_ids = set(self.hits)
        return [
            hit
            for vid, hit in self.hits.items()
            if vid in allowed_version_ids
        ][:top_k]


class LocatorFixture:
    def __init__(self, tmp_path):
        db = UserDB(str(tmp_path / "users.db"))
        db.create_material_schema()
        self.db = db
        self.alice_id = db.create_user("alice", "Password-123!")
        self.hr_id = db.create_user("hr", "Password-123!")
        self.semantic = FakeSemantic()
        self.locator = MaterialLocator(db, semantic_retriever=self.semantic.retrieve)
        self.locator._visible_docs = self._visible_docs
        self._seeded = {}  # version_id -> document_id

    def alice(self):
        return {"id": self.alice_id, "username": "alice", "is_admin": False}

    def _visible_docs(self, db, user):
        visible = {}
        for doc in self.db.list_document_permissions_all():
            full = self.db.get_document_permission_by_id(doc["id"])
            if full is None or full.get("deleted_at"):
                continue
            if full["current_version_id"] is None:
                continue
            if (
                user.get("is_admin")
                or full["owner_id"] == user["id"]
                or full.get("scope") == "public"
                or self.db.is_document_shared(full["id"], user["id"])
            ):
                visible[full["id"]] = full
        return visible

    def seed(
        self,
        filename,
        owner="alice",
        subject_user_id=None,
        document_type="",
        logical_path="",
        ocr_text="",
        shared_with=(),
    ):
        owner_id = self.alice_id if owner == "alice" else self.hr_id
        doc_id = self.db.create_document_permission(filename, "kb-1", owner_id, 1, False)
        if subject_user_id is not None:
            subject_id = self.alice_id if subject_user_id == "alice" else self.hr_id
            subject_user_id = subject_id
        with self.db._lock:
            self.db._conn.execute(
                "UPDATE document_permissions SET subject_user_id=?, document_type=?, logical_path=? "
                "WHERE id=?",
                (subject_user_id, document_type, logical_path, doc_id),
            )
            self.db._conn.commit()
        for share_user in shared_with:
            share_id = self.alice_id if share_user == "alice" else self.hr_id
            self.db.share_document(doc_id, share_id, self.hr_id)
        version_id = self.db.create_document_version(
            doc_id, filename, f"/tmp/{filename}", "hash", owner_id
        )
        self.db.activate_document_version(doc_id, version_id, "kb-1", filename)
        self.db.insert_material_search_fts(
            doc_id, version_id, filename, logical_path, document_type, "[]", ocr_text
        )
        citation = CitationLocation(kind="page_region", value="1")
        self.semantic.add(version_id, doc_id, ocr_text or "内容", citation)
        self._seeded[version_id] = doc_id
        return doc_id

    def locate(self, text, user):
        query = MaterialLocationQuery(text=text)
        return self.locator.locate(query, user)

    def serialize(self, result, user):
        return material_result_response(result, user, self.db)

    @property
    def model_input(self):
        return self.locator.model_input

    def revoke(self, document_id, username):
        self.db._conn.execute(
            "UPDATE document_permissions SET owner_id=?, scope='private' WHERE id=?",
            (self.hr_id, document_id),
        )
        self.db._conn.commit()

    def get(self, url, user):
        parts = url.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "documents":
            document_id = int(parts[1])
        else:
            return SimpleNamespace(status_code=404)
        doc = self.db.get_document_permission_by_id(document_id)
        if doc is None:
            return SimpleNamespace(status_code=404)
        try:
            check_doc_permission(self.db, doc["doc_name"], doc["kb_id"], user, "view")
            return SimpleNamespace(status_code=200)
        except HTTPException as exc:
            return SimpleNamespace(status_code=exc.status_code)


@pytest.fixture
def locator_fixture(tmp_path):
    return LocatorFixture(tmp_path)


def test_locate_my_onboarding_material_combines_exact_and_semantic(locator_fixture):
    f = locator_fixture
    f.seed(
        filename="劳动合同扫描件.pdf",
        subject_user_id="alice",
        document_type="onboarding",
        logical_path="我的人事资料/入职材料",
        ocr_text="保密义务",
    )
    result = f.locate("我入职时签过的保密资料", user=f.alice())
    assert result[0].logical_path == "我的人事资料/入职材料"
    assert result[0].citation.location.kind == "page_region"


def test_invisible_filename_never_reaches_locator_model(locator_fixture):
    f = locator_fixture
    f.seed(
        filename="张三工资调整.pdf",
        owner="hr",
        subject_user_id="hr",
        document_type="salary",
        ocr_text="工资调整",
    )
    f.locate("张三资料", user=f.alice())
    assert "张三工资调整.pdf" not in f.model_input


def test_download_action_is_backend_generated_and_rechecks_acl(locator_fixture):
    f = locator_fixture
    f.seed(filename="我的合同.pdf", subject_user_id="alice", ocr_text="合同内容")
    result = f.locate("我的合同", user=f.alice())[0]
    response = f.serialize(result, user=f.alice())
    assert response["download_url"].startswith("/documents/")
    f.revoke(result.document_id, "alice")
    assert f.get(response["download_url"], user=f.alice()).status_code == 403


def test_location_response_never_contains_physical_path(locator_fixture):
    f = locator_fixture
    f.seed(filename="我的合同.pdf", ocr_text="合同内容")
    result = f.locate("我的合同", user=f.alice())[0]
    response = f.serialize(result, user=f.alice())
    text = str(response)
    assert "file_path" not in text
    assert "qdrant" not in text.lower()
    assert "/tmp/" not in text
