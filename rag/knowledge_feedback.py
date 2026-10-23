"""Structured knowledge feedback with deterministic aggregation keys."""

from __future__ import annotations

import hashlib

from rag.user_db import UserDB


class FeedbackValidationError(Exception):
    """Raised when a feedback submission violates validation rules."""


_PUNCTUATION = str.maketrans({"？": "?", "！": "!", "，": ",", "。": "."})


def normalize_question(text: str) -> str:
    """Normalize without any LLM: fold punctuation, case, and whitespace."""
    normalized = text.translate(_PUNCTUATION).casefold()
    return " ".join(normalized.split())


def question_hash(text: str) -> str:
    return hashlib.sha256(normalize_question(text).encode("utf-8")).hexdigest()


def submit_knowledge_feedback(
    db: UserDB,
    user: dict,
    assistant_message_id: int,
    kind: str,
    citation_id: int | None,
    comment: str,
    suggested_answer: str,
) -> dict:
    """Validate and persist one structured feedback submission."""
    if kind not in ("useful", "not_useful", "outdated"):
        raise FeedbackValidationError(f"invalid feedback kind: {kind}")
    if not db.message_belongs_to_user(assistant_message_id, user["id"]):
        raise FeedbackValidationError("assistant message does not belong to the user")

    citations = db.get_answer_citations(assistant_message_id)
    if kind == "outdated":
        from rag.citation_resolution import resolve_legacy_citation

        def resolve_or_unresolved(citation):
            try:
                return resolve_legacy_citation(db, citation)
            except Exception:  # noqa: BLE001 - uncertain legacy sources are not feedback targets
                return {**citation, "source_kind": "legacy"}

        selected = next((c for c in citations if c.get("id") == citation_id), None)
        if selected is not None:
            selected = resolve_or_unresolved(selected)
            source_kind = selected.get("source_kind") or "legacy"
            if source_kind != "document":
                reason = "unresolved" if source_kind == "legacy" else "wiki"
                raise FeedbackValidationError(
                    f"outdated feedback cannot target {reason} citations"
                )
        elif citation_id is None and any(
            resolve_or_unresolved(citation).get("source_kind") != "document"
            for citation in citations
        ):
            raise FeedbackValidationError(
                "outdated feedback requires an unambiguous document citation"
            )
    if kind == "outdated" and len(citations) > 1 and citation_id is None:
        raise FeedbackValidationError("select a citation for outdated feedback")
    if citation_id is not None and not any(
        c["id"] == citation_id for c in citations
    ):
        raise FeedbackValidationError("citation does not belong to the assistant message")

    question_text = db.get_message_question(assistant_message_id)
    feedback_id = db.add_knowledge_feedback(
        message_id=assistant_message_id,
        user_id=user["id"],
        kind=kind,
        question_text=question_text,
        question_hash=question_hash(question_text),
        citation_id=citation_id,
        suggested_answer=suggested_answer,
        comment=comment,
    )
    # Trigger deterministic task rules in the same application call.
    from rag.knowledge_tasks import TaskService

    TaskService(db).record_feedback(feedback_id)
    return {"feedback_id": feedback_id, "kind": kind}
