"""Unit tests for Knowledge Gap Ticket to Wiki Remediation Loop (Track B)."""

from __future__ import annotations

import pytest

from rag.evolution.gap_remediation import GapRemediationService
from rag.evolution.gaps import KnowledgeGapTicket
from rag.evolution.store import FailureTraceStore
from rag.wiki.store import WikiStore


class FakeRetriever:
    """Mock retrieval returning configured text chunks for probe testing."""

    def __init__(self, texts_by_kb: dict[str, list[str]] | None = None):
        self._texts = texts_by_kb or {}

    def set_texts(self, kb_id: str, texts: list[str]) -> None:
        self._texts[kb_id] = texts

    def retrieve(self, question: str, *, kb_id: str | None = None) -> list:
        class ChunkMock:
            def __init__(self, text: str):
                self.text = text

        texts = self._texts.get(kb_id or "", [])
        return [ChunkMock(t) for t in texts]


@pytest.fixture
def remediation_env(tmp_path):
    trace_db = str(tmp_path / "evolution.db")
    wiki_db = str(tmp_path / "wiki.db")

    trace_store = FailureTraceStore(db_path=trace_db)
    wiki_store = WikiStore(db_path=wiki_db)
    retriever = FakeRetriever()

    service = GapRemediationService(
        trace_store=trace_store,
        wiki_store=wiki_store,
        retrieval_reader=retriever,
    )

    yield {
        "trace_store": trace_store,
        "wiki_store": wiki_store,
        "retriever": retriever,
        "service": service,
    }

    wiki_store.close()


def _create_sample_ticket(
    trace_store: FailureTraceStore,
    kb_id: str = "kb_ops",
    missing_terms: tuple[str, ...] = ("resolv", "conf", "nameserver"),
    question_samples: tuple[str, ...] = ("resolv.conf nameserver",),
) -> int:
    cluster = trace_store.upsert_cluster(
        cluster_key=f"{kb_id}|generation|gap_{abs(hash(missing_terms))}",
        kb_id=kb_id,
        failure_stage="generation",
        root_cause_feature="undetermined",
        seen_at=200.0,
    )
    ticket = KnowledgeGapTicket(
        cluster_id=cluster["id"],
        kb_id=kb_id,
        question_samples=question_samples,
        occurrences=5,
        first_seen=100.0,
        last_seen=200.0,
        missing_terms=missing_terms,
        probed_questions=len(question_samples),
        rationale="Missing terms in corpus",
        created_at=200.0,
        status="open",
    )
    return trace_store.add_gap_ticket(ticket)


def test_link_ticket_to_wiki(remediation_env):
    trace_store: FailureTraceStore = remediation_env["trace_store"]
    wiki_store: WikiStore = remediation_env["wiki_store"]
    service: GapRemediationService = remediation_env["service"]

    ticket_id = _create_sample_ticket(trace_store, kb_id="kb_ops")
    wiki_page = wiki_store.create_page(
        kb_id="kb_ops",
        title="DNS排障手册",
        content="包含 resolv.conf 与 nameserver 配置指引。",
    )

    result = service.link_ticket_to_wiki(
        ticket_id=ticket_id,
        page_id=wiki_page.id,
        author_id="admin_user",
    )

    assert result["ticket_id"] == ticket_id
    assert result["remediation"]["type"] == "wiki"
    assert result["remediation"]["asset_id"] == wiki_page.id
    assert result["remediation"]["author_id"] == "admin_user"

    # Verify stored in database
    row = trace_store.get_gap_ticket(ticket_id)
    assert row is not None
    payload = row["payload"]
    assert payload["remediation"]["type"] == "wiki"
    assert payload["remediation"]["asset_id"] == wiki_page.id


def test_link_ticket_mismatch_kb_raises_error(remediation_env):
    trace_store: FailureTraceStore = remediation_env["trace_store"]
    wiki_store: WikiStore = remediation_env["wiki_store"]
    service: GapRemediationService = remediation_env["service"]

    ticket_id = _create_sample_ticket(trace_store, kb_id="kb_ops")
    finance_page = wiki_store.create_page(
        kb_id="kb_finance",
        title="财务报销规范",
        content="发票报销流程。",
    )

    with pytest.raises(ValueError, match="Knowledge base mismatch"):
        service.link_ticket_to_wiki(ticket_id=ticket_id, page_id=finance_page.id)


def test_recheck_ticket_remediation_full_coverage(remediation_env):
    trace_store: FailureTraceStore = remediation_env["trace_store"]
    wiki_store: WikiStore = remediation_env["wiki_store"]
    retriever: FakeRetriever = remediation_env["retriever"]
    service: GapRemediationService = remediation_env["service"]

    ticket_id = _create_sample_ticket(
        trace_store,
        kb_id="kb_ops",
        missing_terms=("resolv", "conf", "nameserver"),
        question_samples=("resolv.conf nameserver",),
    )
    wiki_page = wiki_store.create_page(
        kb_id="kb_ops",
        title="DNS排障手册",
        content="# 核心配置\n请在 /etc/resolv.conf 中配置 nameserver 8.8.8.8。",
    )
    service.link_ticket_to_wiki(ticket_id=ticket_id, page_id=wiki_page.id)

    # In retriever, the content is now present
    retriever.set_texts("kb_ops", [wiki_page.content])

    recheck_res = service.recheck_ticket(ticket_id)
    assert recheck_res["verdict"] == "covered"
    assert recheck_res["status"] == "content_added"
    assert set(recheck_res["covered_terms"]) == {"resolv", "conf", "nameserver"}
    assert recheck_res["still_missing"] == []

    # Verify persistent status in database
    row = trace_store.get_gap_ticket(ticket_id)
    assert row["status"] == "content_added"


def test_recheck_ticket_partial_coverage(remediation_env):
    trace_store: FailureTraceStore = remediation_env["trace_store"]
    wiki_store: WikiStore = remediation_env["wiki_store"]
    retriever: FakeRetriever = remediation_env["retriever"]
    service: GapRemediationService = remediation_env["service"]

    ticket_id = _create_sample_ticket(
        trace_store,
        kb_id="kb_ops",
        missing_terms=("resolv", "conf", "nameserver"),
        question_samples=("resolv.conf nameserver",),
    )
    wiki_page = wiki_store.create_page(
        kb_id="kb_ops",
        title="网络排错",
        content="包含 resolv.conf 配置，但未涉及 nameserver。",
    )
    service.link_ticket_to_wiki(ticket_id=ticket_id, page_id=wiki_page.id)

    # Retriever only finds resolv.conf
    retriever.set_texts("kb_ops", ["配置 resolv.conf 完成基础解析"])

    recheck_res = service.recheck_ticket(ticket_id)
    assert recheck_res["verdict"] == "partial"
    assert recheck_res["status"] == "open"
    assert "resolv" in recheck_res["covered_terms"]
    assert "nameserver" in recheck_res["still_missing"]

    # Verify persistent status remains open with updated missing list
    row = trace_store.get_gap_ticket(ticket_id)
    assert row["status"] == "open"
    assert "nameserver" in row["payload"]["missing_terms"]


def test_get_ticket_remediation_details(remediation_env):
    trace_store: FailureTraceStore = remediation_env["trace_store"]
    wiki_store: WikiStore = remediation_env["wiki_store"]
    service: GapRemediationService = remediation_env["service"]

    ticket_id = _create_sample_ticket(trace_store, kb_id="kb_ops")
    wiki_page = wiki_store.create_page(
        kb_id="kb_ops",
        title="发布SOP",
        content="SOP内容详情。",
    )
    service.link_ticket_to_wiki(ticket_id=ticket_id, page_id=wiki_page.id, author_id="ops_lead")

    details = service.get_ticket_remediation(ticket_id)
    assert details is not None
    assert details["ticket_id"] == ticket_id
    assert details["asset_type"] == "wiki"
    assert details["asset_id"] == wiki_page.id
    assert details["author_id"] == "ops_lead"
    assert details["page_title"] == "发布SOP"
    assert details["page_slug"] == wiki_page.slug
