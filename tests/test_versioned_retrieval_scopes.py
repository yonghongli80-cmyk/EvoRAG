"""A-09 + 跨域版本可见性：wiki revision 与 document version 是两套 id 空间。

2026-10-22 审查（A-09）：每道题都把 wiki 页面与修订全量扫两遍（current + historical），
且 ``get_revisions`` 逐页拉取正文（N+1）。S4 同时发现一个跨域串权风险：wiki 的
``revision_id`` 与 ``document_versions.id`` 被混进同一个 ``allowed_version_ids`` 集合，
两个 id 空间重号时（wiki revision 7 与 document version 7）持有 wiki 修订 N 的用户会同时
命中 document version N。

因此这里钉住两件事：

* 版本可见性按 ``chunk_kind`` 分成两套集合（wiki 只匹配 wiki 点，文档只匹配
  ``document_version_id``），没有 wiki 授权时 wiki 点 fail-closed；
* 一次检索只扫一遍 wiki，且只读必要列（不拉正文、不逐页拉修订）。
"""

from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient

import rag.versioned_retrieval as versioned_retrieval
from rag.models import Chunk
from rag.retriever import Retriever
from rag.versioned_retrieval import (
    _visible_current_version_ids,
    _visible_version_scopes,
    retrieve_current_then_history,
)

USER = {"id": 1, "username": "alice", "permission_level": 1}
ADMIN = {"id": 99, "username": "root", "permission_level": 0, "is_admin": True}


class _DocDB:
    """One visible document whose current version is 5 and whose history is {4}."""

    def __init__(self):
        self.accessible_calls = 0
        self.version_calls = 0
        self.permission_calls = 0

    def get_accessible_doc_names(self, kb_id, user_id, user_level=1):
        self.accessible_calls += 1
        return ["policy.pdf"]

    def get_document_permission(self, name, kb_id):
        return {"id": 7, "doc_name": name, "kb_id": kb_id, "current_version_id": 5}

    def get_document_permission_by_id(self, document_id):
        self.permission_calls += 1
        return {"id": 7, "doc_name": "policy.pdf", "kb_id": "rag_docs", "current_version_id": 5}

    def list_document_permissions_all(self):
        return [{"id": 7}]

    def list_document_versions(self, document_id):
        self.version_calls += 1
        return [
            {"id": 5, "status": "current", "validity_status": "valid"},
            {"id": 4, "status": "historical", "validity_status": "valid"},
        ]


class _ScopeRecordingRetriever:
    def __init__(self, *, current, history):
        self.current = current
        self.history = history
        self.calls = []

    def retrieve(
        self,
        query,
        top_k=5,
        doc_name=None,
        tags=None,
        allowed_version_ids=None,
        validity_statuses=None,
        allowed_wiki_revision_ids=None,
    ):
        self.calls.append(
            SimpleNamespace(
                allowed_version_ids=allowed_version_ids,
                allowed_wiki_revision_ids=allowed_wiki_revision_ids,
                validity_statuses=validity_statuses,
            )
        )
        if allowed_version_ids and 5 in allowed_version_ids:
            return list(self.current)[:top_k]
        return list(self.history)[:top_k]


class _WikiStore:
    """Public-API-only wiki store double (no ``_conn``): exercises the fallback path."""

    def __init__(self, revisions, *, pages=None):
        self.revisions = revisions  # page_id -> [revision ids]
        self.pages = pages or [SimpleNamespace(id=page_id, current_revision_id=ids[0]) for page_id, ids in revisions.items()]
        self.list_calls = 0
        self.revision_calls = 0

    def list_pages(self, kb_id=None, limit=100, offset=0):
        self.list_calls += 1
        return list(self.pages)

    def get_revisions(self, page_id):
        self.revision_calls += 1
        return [SimpleNamespace(id=revision_id) for revision_id in self.revisions[page_id]]


@pytest.fixture
def wiki_visible(monkeypatch):
    monkeypatch.setattr(versioned_retrieval, "_can_user_view_kb", lambda *_args: True)


# ── 两套 id 空间不能合并 ────────────────────────────────────────────────────


def test_wiki_revision_and_document_version_are_reported_as_separate_scopes(wiki_visible):
    db = _DocDB()
    wiki = _WikiStore({11: [7]})

    current, _historical = _visible_version_scopes(db, USER, "rag_docs", wiki_store=wiki)

    assert current.documents == {5}
    assert current.wiki == {7}
    # 不能出现 "7 既在文档集合又在 wiki 集合" 这种合并后的形态。
    assert 7 not in current.documents


def test_a_wiki_revision_does_not_unlock_a_colliding_document_version(tmp_path, monkeypatch):
    """负向用例：wiki revision 7 可见，document version 7 不可见。"""
    import rag.bm25_store as bm25_store
    import rag.retriever as retriever_module
    import rag.vector_store as vs

    monkeypatch.setattr(bm25_store.settings, "bm25_db_path", str(tmp_path / "bm25.db"))
    monkeypatch.setattr(retriever_module.settings, "bm25_db_path", str(tmp_path / "bm25.db"))
    retriever_module.invalidate_bm25_cache()
    client = QdrantClient(path=str(tmp_path / "qdrant"))
    monkeypatch.setattr(vs, "_client", client)
    monkeypatch.setattr(retriever_module, "embed", lambda _texts: [[0.1] * 1024])
    try:
        vs._ensure_collection_name("kb_collision")
        wiki_chunk = Chunk(
            text="wiki 运行手册", doc_name="Runbook", chunk_index=0,
            document_id=42, document_version_id=7, chunk_kind="wiki",
        )
        document_chunk = Chunk(
            text="机密文档内容", doc_name="secret.md", chunk_index=0,
            document_id=7, document_version_id=7,
        )
        vs.add_to_collection(
            "kb_collision",
            [wiki_chunk, document_chunk],
            [[0.1] * 1024, [0.1] * 1024],
            document_version_id=7,
        )

        retriever = Retriever([], collection_name="kb_collision")
        hits = retriever.retrieve(
            "运行手册",
            top_k=5,
            allowed_version_ids={999},  # 文档版本：7 不可见
            allowed_wiki_revision_ids={7},  # wiki 修订：7 可见
        )
    finally:
        retriever_module.invalidate_bm25_cache()

    assert [chunk.text for chunk in hits] == ["wiki 运行手册"]


def test_a_document_version_does_not_unlock_a_colliding_wiki_revision(tmp_path, monkeypatch):
    """反向负向用例：document version 7 可见，wiki revision 7 不可见。"""
    import rag.bm25_store as bm25_store
    import rag.retriever as retriever_module
    import rag.vector_store as vs

    monkeypatch.setattr(bm25_store.settings, "bm25_db_path", str(tmp_path / "bm25.db"))
    monkeypatch.setattr(retriever_module.settings, "bm25_db_path", str(tmp_path / "bm25.db"))
    retriever_module.invalidate_bm25_cache()
    client = QdrantClient(path=str(tmp_path / "qdrant"))
    monkeypatch.setattr(vs, "_client", client)
    monkeypatch.setattr(retriever_module, "embed", lambda _texts: [[0.1] * 1024])
    try:
        vs._ensure_collection_name("kb_collision2")
        wiki_chunk = Chunk(
            text="wiki 运行手册", doc_name="Runbook", chunk_index=0,
            document_id=42, document_version_id=7, chunk_kind="wiki",
        )
        document_chunk = Chunk(
            text="公开文档内容", doc_name="public.md", chunk_index=0,
            document_id=7, document_version_id=7,
        )
        vs.add_to_collection(
            "kb_collision2",
            [wiki_chunk, document_chunk],
            [[0.1] * 1024, [0.1] * 1024],
            document_version_id=7,
        )

        retriever = Retriever([], collection_name="kb_collision2")
        hits = retriever.retrieve(
            "运行手册",
            top_k=5,
            allowed_version_ids={7},
            allowed_wiki_revision_ids={999},
        )
    finally:
        retriever_module.invalidate_bm25_cache()

    assert [chunk.text for chunk in hits] == ["公开文档内容"]


def test_wiki_chunks_are_excluded_when_only_a_document_scope_is_given(tmp_path, monkeypatch):
    """只给文档版本过滤、没有 wiki 授权信号时，wiki 点必须 fail-closed。

    （``documents``/比较工具就是这样调用的：它们从未计算过 wiki 可见性。）
    """
    import rag.bm25_store as bm25_store
    import rag.retriever as retriever_module
    import rag.vector_store as vs

    monkeypatch.setattr(bm25_store.settings, "bm25_db_path", str(tmp_path / "bm25.db"))
    monkeypatch.setattr(retriever_module.settings, "bm25_db_path", str(tmp_path / "bm25.db"))
    retriever_module.invalidate_bm25_cache()
    client = QdrantClient(path=str(tmp_path / "qdrant"))
    monkeypatch.setattr(vs, "_client", client)
    monkeypatch.setattr(retriever_module, "embed", lambda _texts: [[0.1] * 1024])
    try:
        vs._ensure_collection_name("kb_doconly")
        wiki_chunk = Chunk(
            text="wiki 运行手册", doc_name="Runbook", chunk_index=0,
            document_id=42, document_version_id=7, chunk_kind="wiki",
        )
        document_chunk = Chunk(
            text="政策文档", doc_name="policy.md", chunk_index=0,
            document_id=7, document_version_id=7,
        )
        vs.add_to_collection(
            "kb_doconly",
            [wiki_chunk, document_chunk],
            [[0.1] * 1024, [0.1] * 1024],
            document_version_id=7,
        )

        retriever = Retriever([], collection_name="kb_doconly")
        hits = retriever.retrieve("运行手册", top_k=5, allowed_version_ids={7})
    finally:
        retriever_module.invalidate_bm25_cache()

    assert [chunk.text for chunk in hits] == ["政策文档"]


def test_retrieve_current_then_history_passes_both_scopes(wiki_visible):
    db = _DocDB()
    wiki = _WikiStore({11: [7]})
    retriever = _ScopeRecordingRetriever(
        current=[Chunk(text="current", doc_name="policy.pdf", chunk_index=0, document_id=7, document_version_id=5)],
        history=[Chunk(text="old", doc_name="policy.pdf", chunk_index=0, document_id=7, document_version_id=4)],
    )

    retrieve_current_then_history(
        retriever, "policy", top_k=3, user=USER, user_db=db, kb_id="rag_docs", wiki_store=wiki
    )

    first = retriever.calls[0]
    assert first.allowed_version_ids == {5}
    assert first.allowed_wiki_revision_ids == {7}


def test_admin_scopes_stay_current_first_and_wiki_scoped(wiki_visible):
    current, historical = _visible_version_scopes(
        _DocDB(), ADMIN, "rag_docs", wiki_store=_WikiStore({11: [7]})
    )

    assert current.documents == {5}
    assert current.wiki == {7}
    assert historical.documents == {4}
    assert historical.wiki == set()


# ── A-09：一次请求只扫一遍 wiki，且只读必要列 ───────────────────────────────


def test_wiki_visibility_reads_only_the_revision_column(tmp_path, wiki_visible, monkeypatch):
    """真实 WikiStore：只查 current_revision_id，不拉正文、不逐页拉修订。"""
    from rag.wiki.store import WikiStore

    store = WikiStore(db_path=str(tmp_path / "wiki.db"))
    try:
        page = store.create_page(kb_id="rag_docs", title="Runbook", content="# Runbook\n\n正文")
        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)
        monkeypatch.setattr(
            store, "list_pages", lambda *a, **k: (_ for _ in ()).throw(AssertionError("full page scan"))
        )
        monkeypatch.setattr(
            store, "get_revisions", lambda *a, **k: (_ for _ in ()).throw(AssertionError("N+1 revision scan"))
        )

        ids = _visible_current_version_ids(_DocDB(), USER, "rag_docs", wiki_store=store)

        assert page.current_revision_id in ids
        page_queries = [sql for sql in statements if "wiki_pages" in sql.lower()]
        assert len(page_queries) == 1, statements
        assert all("content" not in sql.lower() for sql in page_queries), page_queries
        assert not [sql for sql in statements if "wiki_revisions" in sql.lower()], statements
    finally:
        store.close()


def test_public_api_fallback_scans_pages_once_without_per_page_revisions(wiki_visible):
    """没有 SQLite 连接可读的 store（测试替身/未来实现）退化为一次分页扫描，不做 N+1。"""
    wiki = _WikiStore({11: [7], 12: [8]})

    ids = _visible_current_version_ids(_DocDB(), USER, "rag_docs", wiki_store=wiki)

    # 合并视图仍包含文档当前版本（兼容旧调用方），但 wiki 修订只经一次分页扫描取得。
    assert ids == {5, 7, 8}
    assert wiki.list_calls == 1
    assert wiki.revision_calls == 0


def test_a_single_retrieval_computes_visibility_once(wiki_visible):
    db = _DocDB()
    wiki = _WikiStore({11: [7]})
    retriever = _ScopeRecordingRetriever(
        current=[Chunk(text="current", doc_name="policy.pdf", chunk_index=0, document_id=7, document_version_id=5)],
        history=[],
    )

    retrieve_current_then_history(
        retriever, "policy", top_k=1, user=USER, user_db=db, kb_id="rag_docs", wiki_store=wiki
    )

    # current 与 historical 共用同一次可见性计算：文档权限与版本扫描各一遍。
    assert db.accessible_calls == 1
    assert db.version_calls == 1
    assert wiki.list_calls == 1
    assert wiki.revision_calls == 0


def test_historical_wiki_revisions_are_not_reported(wiki_visible):
    """wiki 只索引当前修订：历史 revision 永远匹配不到向量，不该再逐页去取。"""
    _current, historical = _visible_version_scopes(_DocDB(), USER, "rag_docs", wiki_store=_WikiStore({11: [7]}))

    assert historical.wiki == set()


def test_no_visible_scope_returns_empty_without_retrieval():
    class _NoAccessDB(_DocDB):
        def get_accessible_doc_names(self, kb_id, user_id, user_level=1):
            return []

        def get_document_permission_by_id(self, document_id):
            return None

    class _FailRetriever:
        def retrieve(self, *_args, **_kwargs):
            raise AssertionError("unrestricted retrieval must not run")

    result = retrieve_current_then_history(
        _FailRetriever(), "policy", top_k=3, user=USER, user_db=_NoAccessDB(), kb_id="rag_docs"
    )

    assert result == []
