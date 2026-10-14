"""Knowledge Gap Ticket remediation service (Track B).

Connects diagnosed gap tickets to remediation assets (e.g. Hypertext Wiki pages)
and drives deterministic re-checking to close the loop (status -> content_added).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from config import settings
from rag.evolution.gaps import (
    DEFAULT_PROBE_LIMIT,
    aggregate_missing_terms,
    missing_terms_for,
    ticket_from_payload,
)
from rag.evolution.store import FailureTraceStore
from rag.wiki.store import WikiStore

logger = logging.getLogger(__name__)


class GapRemediationService:
    """Orchestrates gap ticket linking to knowledge assets and verification rechecks."""

    def __init__(
        self,
        trace_store: FailureTraceStore,
        wiki_store: WikiStore | None = None,
        retrieval_reader: Any | None = None,
    ) -> None:
        self._trace_store = trace_store
        self._wiki_store = wiki_store or WikiStore()
        self._reader = retrieval_reader

    def _ensure_reader(self) -> Any:
        if self._reader is not None:
            return self._reader
        from rag.evolution.worker import AdminReadOnlyRetrievalReader

        self._reader = AdminReadOnlyRetrievalReader(settings.users_db_path)
        return self._reader

    def link_ticket_to_wiki(
        self,
        ticket_id: int,
        page_id: int,
        author_id: str | None = None,
    ) -> dict[str, Any]:
        """Link a knowledge gap ticket to a Wiki page providing the missing knowledge."""
        ticket_row = self._trace_store.get_gap_ticket(ticket_id)
        if not ticket_row:
            raise ValueError(f"KnowledgeGapTicket with ID {ticket_id} not found")

        wiki_page = self._wiki_store.get_page(page_id)
        if not wiki_page:
            raise ValueError(f"WikiPage with ID {page_id} not found")

        ticket_kb = str(ticket_row.get("kb_id") or "")
        if ticket_kb != wiki_page.kb_id:
            raise ValueError(
                f"Knowledge base mismatch: ticket is for '{ticket_kb}' but wiki page is in '{wiki_page.kb_id}'"
            )

        ticket_obj = ticket_from_payload(ticket_row.get("payload") or {})
        updated_ticket = ticket_obj.with_remediation(
            asset_type="wiki",
            asset_id=page_id,
            author_id=author_id,
        )

        self._trace_store.update_gap_ticket(ticket_id, updated_ticket)
        logger.info(
            "gap_ticket_linked_to_wiki ticket=%s page=%s author=%s",
            ticket_id,
            page_id,
            author_id,
        )
        return {
            "ticket_id": ticket_id,
            "status": updated_ticket.status,
            "remediation": updated_ticket.remediation,
        }

    def recheck_ticket(
        self,
        ticket_id: int,
        *,
        probe_limit: int = DEFAULT_PROBE_LIMIT,
    ) -> dict[str, Any]:
        """Execute a deterministic re-retrieval probe on the ticket's questions."""
        ticket_row = self._trace_store.get_gap_ticket(ticket_id)
        if not ticket_row:
            raise ValueError(f"KnowledgeGapTicket with ID {ticket_id} not found")

        payload = ticket_row.get("payload") or {}
        ticket_obj = ticket_from_payload(payload)
        questions = list(ticket_obj.question_samples[:probe_limit])
        if not questions:
            return {
                "ticket_id": ticket_id,
                "verdict": "unchanged",
                "status": ticket_obj.status,
                "covered_terms": [],
                "still_missing": list(ticket_obj.missing_terms),
            }

        reader = self._ensure_reader()
        per_question: list[tuple[str, ...]] = []
        probed = 0

        for q in questions:
            try:
                chunks = reader.retrieve(q, kb_id=ticket_obj.kb_id) or []
            except Exception:
                logger.exception("gap_ticket_recheck_probe_failed kb=%s q=%s", ticket_obj.kb_id, q)
                continue
            probed += 1
            texts = [getattr(c, "text", "") or "" for c in chunks]
            per_question.append(missing_terms_for(q, texts))

        missing_now = aggregate_missing_terms(per_question)
        covered = [term for term in ticket_obj.missing_terms if term not in set(missing_now)]
        now = time.time()

        updated_ticket = ticket_obj.with_recheck(
            missing_terms=missing_now,
            covered_terms=covered,
            probed=probed,
            now=now,
        )

        self._trace_store.record_gap_recheck(ticket_id, updated_ticket, now=now)
        logger.info(
            "gap_ticket_rechecked ticket=%s verdict=%s status=%s still_missing=%s",
            ticket_id,
            (updated_ticket.recheck or {}).get("verdict"),
            updated_ticket.status,
            ",".join(updated_ticket.missing_terms[:4]),
        )

        return {
            "ticket_id": ticket_id,
            "verdict": (updated_ticket.recheck or {}).get("verdict"),
            "status": updated_ticket.status,
            "covered_terms": covered,
            "still_missing": list(missing_now),
        }

    def get_ticket_remediation(self, ticket_id: int) -> dict[str, Any] | None:
        """Fetch remediation metadata and resolved asset information."""
        ticket_row = self._trace_store.get_gap_ticket(ticket_id)
        if not ticket_row:
            return None

        remediation = (ticket_row.get("payload") or {}).get("remediation")
        if not remediation:
            return None

        result: dict[str, Any] = {
            "ticket_id": ticket_id,
            "asset_type": remediation.get("type"),
            "asset_id": remediation.get("asset_id"),
            "author_id": remediation.get("author_id"),
            "linked_at": remediation.get("linked_at"),
        }

        if remediation.get("type") == "wiki":
            page = self._wiki_store.get_page(remediation["asset_id"])
            if page:
                result["page_title"] = page.title
                result["page_slug"] = page.slug
                result["current_revision_id"] = page.current_revision_id

        return result
