"""Task 6: incremental units, bounded embedding cache, and atomic worker."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag.materials.embedding_cache import EmbeddingCache
from rag.materials.formats import build_default_format_registry
from rag.materials.incremental import plan_incremental_index, unit_fingerprint
from rag.materials.models import CitationLocation, ExtractedMaterial, ExtractedUnit
from rag.materials.worker import ActivationFailed, WorkerServices, process_index_job
from rag.user_db import UserDB

REGISTRY = build_default_format_registry()


def extracted(unit_texts, version_id=1, parser="v1"):
    units = tuple(
        ExtractedUnit(
            unit_key=f"u{i}",
            text=text,
            location=CitationLocation(kind="paragraph", value=str(i)),
            source="native_text",
            confidence=1.0,
        )
        for i, text in enumerate(unit_texts)
    )
    return ExtractedMaterial(
        version_id=version_id, parser_schema_version=parser, units=units
    )


class RecordingEmbedder:
    def __init__(self):
        self.texts = []

    def __call__(self, texts):
        self.texts.extend(texts)
        return [[float(len(t)) + 1.0] * 4 for t in texts]


class IncrementalFixture:
    def __init__(self, tmp_path):
        self.embedder = RecordingEmbedder()
        self.cache = EmbeddingCache(tmp_path / "cache.sqlite3")

    def index(self, previous, current):
        plan = plan_incremental_index(previous, current, current.parser_schema_version)
        changed_keys = {fp.unit_key for fp in plan.changed}
        texts = [u.text for u in current.units if u.unit_key in changed_keys]
        fingerprints = [
            unit_fingerprint(t, current.parser_schema_version) for t in texts
        ]
        if texts:
            self.cache.get_or_embed(
                fingerprints, texts, "bge-m3", 1024, self.embedder
            )
        return SimpleNamespace(
            reused_units=len(plan.unchanged), embedded_units=len(plan.changed)
        )


def test_one_changed_unit_embeds_only_that_unit(tmp_path):
    fixture = IncrementalFixture(tmp_path)
    previous = extracted(["unchanged", "old"])
    current = extracted(["unchanged", "new"])
    result = fixture.index(previous, current)
    assert fixture.embedder.texts == ["new"]
    assert result.reused_units == 1
    assert result.embedded_units == 1


class CacheEmbedder:
    def __init__(self):
        self.calls = 0

    def __call__(self, texts):
        self.calls += 1
        return [[float(self.calls)] * 1024 for _ in texts]


class CacheFixture:
    def __init__(self, tmp_path):
        self.embedder = CacheEmbedder()
        self.cache = EmbeddingCache(tmp_path / "cache.sqlite3")

    def embed(self, text, model, dimension, parser):
        fingerprint = unit_fingerprint(text, parser)
        return self.cache.get_or_embed(
            [fingerprint], [text], model, dimension, self.embedder
        )[0]


def test_cache_key_includes_model_dimension_and_parser(tmp_path):
    fixture = CacheFixture(tmp_path)
    first = fixture.embed("same", model="bge-m3", dimension=1024, parser="v1")
    second = fixture.embed("same", model="bge-m3", dimension=1024, parser="v1")
    third = fixture.embed("same", model="bge-m3", dimension=1024, parser="v2")
    assert first == second
    assert fixture.embedder.calls == 2
    assert third != first


class FakeVectorStore:
    def __init__(self):
        self._versions = {}

    def add_units(self, kb_id, chunks, vectors, *, document_id, document_version_id, version_no):
        self._versions.setdefault(document_version_id, []).extend(chunks)

    def delete_version(self, kb_id, version_id):
        self._versions.pop(version_id, None)

    def mark_current(self, kb_id, document_id, version_id):
        pass

    def version_ids(self):
        return set(self._versions)


class WorkerDB(UserDB):
    def __init__(self, path):
        super().__init__(path)
        self._fail_activation = False

    def fail_activation(self):
        self._fail_activation = True

    def _activate_document_version_in_tx(self, *args, **kwargs):
        if self._fail_activation:
            raise ActivationFailed("injected activation failure")
        return super()._activate_document_version_in_tx(*args, **kwargs)


@pytest.fixture
def worker_fixture(tmp_path):
    db = WorkerDB(str(tmp_path / "users.db"))
    db.create_material_schema()
    user_id = db.create_user("alice", "Password-123!")
    document_id = db.create_document_permission("contract.pdf", "kb-1", user_id)
    versions_root = tmp_path / "versions"
    versions_root.mkdir()
    vector_store = FakeVectorStore()

    def extractor(path, version_id, resolution):
        # Content varies per version so later versions change units.
        return extracted([f"合同正文 v{version_id}"], version_id=version_id)

    fixture = SimpleNamespace()
    fixture.db = db
    fixture.vector_store = vector_store
    fixture.versions_root = versions_root
    fixture.user_id = user_id
    fixture.document_id = document_id

    def process(job_dict):
        services = WorkerServices(
            db=db,
            versions_root=versions_root,
            registry=REGISTRY,
            vector_store=vector_store,
            embedder=RecordingEmbedder(),
            cache=EmbeddingCache(tmp_path / "cache.sqlite3"),
            extractor=extractor,
        )
        return process_index_job(job_dict, services)

    fixture.process = process
    fixture.job = lambda version_id, job_id=1: {
        "id": job_id,
        "document_id": fixture.document_id,
        "version_id": version_id,
        "content_hash": f"hash-{version_id}",
        "status": "running",
    }
    return fixture


def test_stale_job_cannot_activate_over_newer_version(worker_fixture):
    db = worker_fixture.db
    doc = worker_fixture.document_id
    v11 = db.create_document_version(
        doc, "contract.pdf", str(worker_fixture.versions_root / "v11.bin"), "h11", worker_fixture.user_id
    )
    db.create_document_version(
        doc, "contract.pdf", str(worker_fixture.versions_root / "v12.bin"), "h12", worker_fixture.user_id
    )
    result = worker_fixture.process(worker_fixture.job(v11))
    assert result.status == "cancelled"
    assert db.current_version_id(doc) != v11


def test_sqlite_failure_removes_only_new_points(worker_fixture):
    db = worker_fixture.db
    doc = worker_fixture.document_id
    v11 = db.create_document_version(
        doc, "contract.pdf", str(worker_fixture.versions_root / "v11.bin"), "h11", worker_fixture.user_id
    )
    result = worker_fixture.process(worker_fixture.job(v11))
    assert result.status == "done"
    assert worker_fixture.vector_store.version_ids() == {v11}
    assert db.current_version_id(doc) == v11

    v12 = db.create_document_version(
        doc, "contract.pdf", str(worker_fixture.versions_root / "v12.bin"), "h12", worker_fixture.user_id
    )
    db.fail_activation()
    with pytest.raises(ActivationFailed):
        worker_fixture.process(worker_fixture.job(v12))
    assert worker_fixture.vector_store.version_ids() == {v11}
    assert db.current_version_id(doc) == v11


def test_successful_job_indexes_and_activates(worker_fixture):
    db = worker_fixture.db
    doc = worker_fixture.document_id
    v11 = db.create_document_version(
        doc, "contract.pdf", str(worker_fixture.versions_root / "v11.bin"), "h11", worker_fixture.user_id
    )
    result = worker_fixture.process(worker_fixture.job(v11))
    assert result.status == "done"
    assert result.embedded_units == 1
    assert db.current_version_id(doc) == v11
    # FTS row for the activated version is searchable.
    hits = db.search_material_search_fts("合同正文")
    assert {h["document_id"] for h in hits} == {doc}
    assert hits[0]["version_id"] == v11
