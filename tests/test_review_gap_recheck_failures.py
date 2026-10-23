"""Failed or incomplete retrieval probes must never close knowledge gap tickets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag.evolution.gap_remediation import GapRemediationService
from rag.evolution.gaps import KnowledgeGapTicket, probe_questions
from rag.evolution.models import FailureObservation, FailureStage
from rag.evolution.store import FailureTraceStore


class _Chunk:
    def __init__(self, text: str):
        self.text = text


class _Retriever:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.closed = False

    def retrieve(self, question: str, *, kb_id: str):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return [_Chunk(text) for text in outcome]

    def close(self):
        self.closed = True


def _ticket(store: FailureTraceStore, questions=("resolv.conf nameserver",)) -> int:
    cluster = store.upsert_cluster(
        cluster_key=f"kb|generation|{len(store.list_gap_tickets())}|{len(questions)}",
        kb_id="kb",
        failure_stage="generation",
        root_cause_feature="undetermined",
        seen_at=10.0,
    )
    ticket = KnowledgeGapTicket(
        cluster_id=cluster["id"],
        kb_id="kb",
        question_samples=tuple(questions),
        occurrences=2,
        first_seen=1.0,
        last_seen=10.0,
        missing_terms=("resolv", "conf", "nameserver"),
        probed_questions=len(questions),
        rationale="missing terms",
        created_at=10.0,
        status="open",
    )
    return store.add_gap_ticket(ticket)


@pytest.fixture
def remediation(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    yield store
    store.close()


def _service(store, reader):
    return GapRemediationService(trace_store=store, wiki_store=object(), retrieval_reader=reader)


def test_all_probe_failures_leave_ticket_open_and_terms_unchanged(remediation):
    ticket_id = _ticket(remediation)
    reader = _Retriever([TimeoutError("reader unavailable")])
    service = _service(remediation, reader)

    result = service.recheck_ticket(ticket_id)

    row = remediation.get_gap_ticket(ticket_id)
    assert result["verdict"] == "inconclusive"
    assert result["status"] == "open"
    assert result["covered_terms"] == []
    assert result["still_missing"] == ["resolv", "conf", "nameserver"]
    assert row["status"] == "open"
    assert row["payload"]["missing_terms"] == ["resolv", "conf", "nameserver"]
    assert row["payload"]["recheck"]["probed"] == 0


def test_partial_probe_failure_cannot_close_or_claim_failed_terms_covered(remediation):
    ticket_id = _ticket(remediation, ("resolv.conf nameserver", "nameserver router"))
    reader = _Retriever([["resolv.conf nameserver router"], TimeoutError("timeout")])
    service = _service(remediation, reader)

    result = service.recheck_ticket(ticket_id)

    assert result["verdict"] == "inconclusive"
    assert result["status"] == "open"
    assert result["covered_terms"] == []
    assert set(result["still_missing"]) >= {"resolv", "conf", "nameserver"}


def test_probe_limit_marks_unprobed_questions_inconclusive(remediation):
    ticket_id = _ticket(remediation, ("resolv.conf nameserver", "nameserver router"))
    reader = _Retriever([["resolv.conf nameserver router"]])
    service = _service(remediation, reader)

    result = service.recheck_ticket(ticket_id, probe_limit=1)

    assert reader.calls == 1
    assert result["verdict"] == "inconclusive"
    assert result["status"] == "open"
    assert result["covered_terms"] == []
    assert set(result["still_missing"]) >= {"resolv", "conf", "nameserver"}


@pytest.mark.parametrize("probe_limit", [0, -1])
def test_nonpositive_probe_limit_keeps_existing_ticket_state(remediation, probe_limit):
    ticket_id = _ticket(remediation)
    reader = _Retriever([])
    service = _service(remediation, reader)

    result = service.recheck_ticket(ticket_id, probe_limit=probe_limit)

    assert reader.calls == 0
    assert result["verdict"] == "inconclusive"
    assert result["status"] == "open"
    assert result["still_missing"] == ["resolv", "conf", "nameserver"]


def test_later_complete_recheck_can_close_after_an_inconclusive_attempt(remediation):
    ticket_id = _ticket(remediation)
    first = _service(remediation, _Retriever([TimeoutError("temporary")]))
    first.recheck_ticket(ticket_id)
    recovered = _service(remediation, _Retriever([["resolv.conf nameserver"]]))

    result = recovered.recheck_ticket(ticket_id)

    assert result["verdict"] == "covered"
    assert result["status"] == "content_added"
    assert result["still_missing"] == []


def test_aggregate_limit_cannot_drop_an_old_term_still_absent():
    result = probe_questions(
        ["legacy_missing_term"],
        lambda _question: [_Chunk("alpha beta")],
        preserve_terms=("legacy_missing_term",),
    )

    assert result.complete
    assert "legacy_missing_term" in result.missing_terms


def test_all_old_terms_survive_the_new_term_display_limit():
    old_terms = tuple(f"legacyterm{index}" for index in range(14))
    questions = [" ".join(old_terms[:7]), " ".join(old_terms[7:])]
    result = probe_questions(
        questions,
        lambda _question: [],
        preserve_terms=old_terms,
    )

    assert result.complete
    assert set(result.missing_terms) >= set(old_terms)


def test_old_term_missing_for_one_question_survives_presence_in_another():
    outcomes = {
        "nameserver missing here": [],
        "nameserver present here": ["nameserver is documented"],
    }
    result = probe_questions(
        list(outcomes),
        lambda question: [_Chunk(text) for text in outcomes[question]],
        preserve_terms=("nameserver",),
    )

    assert result.complete
    assert "nameserver" in result.missing_terms


def test_recheck_preserves_remediation_link_written_by_another_connection(remediation, tmp_path):
    ticket_id = _ticket(remediation)
    other_connection = FailureTraceStore(tmp_path / "evolution.db")
    page = SimpleNamespace(id=77, kb_id="kb")

    class Wiki:
        def get_page(self, page_id):
            return page if page_id == page.id else None

    linker = GapRemediationService(trace_store=other_connection, wiki_store=Wiki())

    class LinkingReader(_Retriever):
        def retrieve(self, question: str, *, kb_id: str):
            linker.link_ticket_to_wiki(ticket_id, page.id, author_id="admin")
            return [_Chunk("resolv.conf nameserver")]

    service = GapRemediationService(
        trace_store=remediation, wiki_store=Wiki(), retrieval_reader=LinkingReader([])
    )
    service.recheck_ticket(ticket_id)

    row = remediation.get_gap_ticket(ticket_id)
    assert row["payload"]["remediation"]["asset_id"] == page.id
    assert row["payload"]["remediation"]["author_id"] == "admin"
    other_connection.close()


def test_service_close_only_closes_resources_it_created(remediation, monkeypatch):
    injected = _Retriever([])
    service = _service(remediation, injected)
    service.close()
    assert not injected.closed

    class OwnedReader(_Retriever):
        pass

    owned = OwnedReader([])
    monkeypatch.setattr(
        "rag.evolution.worker.AdminReadOnlyRetrievalReader",
        lambda _path: owned,
    )
    service_with_owned_reader = GapRemediationService(trace_store=remediation, wiki_store=object())
    service_with_owned_reader._ensure_reader()
    service_with_owned_reader.close()
    assert owned.closed

    class OwnedStore:
        closed = False

        def close(self):
            self.closed = True

    owned_store = OwnedStore()
    monkeypatch.setattr("rag.evolution.gap_remediation.WikiStore", lambda: owned_store)
    service_with_owned_store = GapRemediationService(trace_store=remediation)
    service_with_owned_store.close()
    assert owned_store.closed


def test_worker_ticket_creation_retains_terms_when_all_initial_probes_fail(tmp_path):
    from rag.evolution.worker import make_evolution_gap_ticket_step

    store = FailureTraceStore(tmp_path / "worker.db")
    cluster = store.upsert_cluster(
        cluster_key="kb|generation|worker-gap",
        kb_id="kb",
        failure_stage="generation",
        root_cause_feature="undetermined",
        seen_at=10.0,
    )
    observation = store.add_observation(
        FailureObservation(
            kb_id="kb",
            question="resolv.conf nameserver",
            route="chat",
            failure_stage=FailureStage.GENERATION,
            root_cause_feature="undetermined",
            created_at=10.0,
            updated_at=10.0,
        )
    )
    store.attach_cluster_member(
        observation["id"], cluster["id"], failure_stage="generation",
        root_cause_feature="undetermined", now=10.0,
    )
    reader = _Retriever([TimeoutError("reader unavailable")])
    step = make_evolution_gap_ticket_step(
        store,
        settings_obj=SimpleNamespace(evolution_diagnosis_labels_path=""),
        retrieval_factory=lambda: reader,
    )

    step()

    ticket = store.list_gap_tickets()[0]
    assert set(ticket["payload"]["missing_terms"]) >= {"resolv", "conf", "nameserver"}
    assert ticket["payload"]["probed_questions"] == 0
    assert ticket["status"] == "open"
    assert ticket["payload"]["recheck"]["verdict"] == "inconclusive"
    store.close()
