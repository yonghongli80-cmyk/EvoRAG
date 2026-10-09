"""Knowledge-gap tickets: the third artifact, and the only one a human writes content for.

Measured motivation: 78% of the recorded failures are the corpus simply not holding the answer
(57 of 73 distinct questions), while the loop's only actuators were prompt and code changes — an
``undetermined`` cluster therefore stopped at the observation queue with nothing to do. A ticket
is **not** a candidate: it never serves traffic, it never enters the candidate pool, and that
pool's invariant (an undetermined root cause never leaves the observation queue) is untouched.
What a ticket does is turn a pile of identical refusals into one reviewable request for content,
in the place where content is actually added.

Drafting is deterministic and costs no model call, because everything a content editor needs is
already recorded: which questions failed, how often, over what window, in which knowledge base,
and — the actionable part — which of their terms the retrieval window never contains. That last
part says *what to write*, rather than merely that something is missing. It is a coverage signal,
not a verdict: a term may be absent while the fact is phrased differently, which is exactly the
kind of thing the ticket asks a human to judge.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# Quoted from the evidence module on purpose: the term rules must be identical to the ones the
# diagnosis already renders, and duplicating them is how two copies drift apart.
from rag.evolution.evidence import _question_terms

#: How many distinct questions a ticket lists (a 112-member cluster needs a sample, not a dump).
DEFAULT_QUESTION_LIMIT = 8

#: How many absent terms a ticket names, most frequent first.
DEFAULT_TERM_LIMIT = 12

#: How many questions one ticket's coverage probe may re-retrieve.
DEFAULT_PROBE_LIMIT = 8


@dataclass(frozen=True)
class KnowledgeGapTicket:
    """One knowledge base's missing content, as a request a person can act on."""

    cluster_id: int
    kb_id: str
    question_samples: tuple[str, ...]
    occurrences: int
    first_seen: float
    last_seen: float
    missing_terms: tuple[str, ...]
    probed_questions: int
    rationale: str
    created_at: float

    def to_payload(self) -> dict:
        """The JSON-safe form the store keeps and an endpoint would render."""
        return {
            "cluster_id": self.cluster_id,
            "kb_id": self.kb_id,
            "question_samples": list(self.question_samples),
            "occurrences": self.occurrences,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "missing_terms": list(self.missing_terms),
            "probed_questions": self.probed_questions,
            "rationale": self.rationale,
            "created_at": self.created_at,
        }


def question_samples(observations: Sequence[Mapping], *, limit: int = DEFAULT_QUESTION_LIMIT) -> tuple[str, ...]:
    """The distinct questions behind a cluster, in first-appearance order, capped.

    Whitespace is normalized so the same question asked with different spacing counts once, and
    the cap keeps a hundred-member cluster reviewable instead of turning the ticket into a dump.
    """
    seen: list[str] = []
    keys: set[str] = set()
    for observation in observations:
        question = " ".join(str(observation.get("question") or "").split())
        if not question or question in keys:
            continue
        keys.add(question)
        seen.append(question)
        if len(seen) >= limit:
            break
    return tuple(seen)


def missing_terms_for(question: str, texts: Sequence[str], *, limit: int = DEFAULT_TERM_LIMIT) -> tuple[str, ...]:
    """The question's terms that appear in none of the recalled texts."""
    terms = _question_terms(question)
    # 与证据模块同一规则：术语小写、语料里型号多为大写，必须大小写不敏感地比对。
    lowered = [(text or "").lower() for text in texts]
    absent = [term for term in terms if not any(term in text for text in lowered)]
    return tuple(absent[:limit])


def aggregate_missing_terms(
    per_question: Iterable[Sequence[str]], *, limit: int = DEFAULT_TERM_LIMIT
) -> tuple[str, ...]:
    """The absent terms across several questions, most-asked first, ties by first appearance."""
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for index, terms in enumerate(per_question):
        for term in terms:
            counts[term] = counts.get(term, 0) + 1
            order.setdefault(term, index)
    ranked = sorted(counts, key=lambda term: (-counts[term], order[term], term))
    return tuple(ranked[:limit])


def build_ticket(
    cluster: Mapping,
    observations: Sequence[Mapping],
    *,
    missing_terms: Sequence[str] = (),
    probed_questions: int = 0,
    question_limit: int = DEFAULT_QUESTION_LIMIT,
    now: float | None = None,
) -> KnowledgeGapTicket:
    """Draft one ticket from a cluster and its member observations; never calls a model.

    ``observations`` are the cluster's members as stored. The rationale states the numbers and
    the absent terms so a reviewer can judge it without opening the database, and says plainly
    that the terms are a coverage signal rather than a verdict.
    """
    samples = question_samples(observations, limit=question_limit)
    stamps = [float(row.get("created_at") or 0.0) for row in observations]
    first_seen = min(stamps) if stamps else 0.0
    last_seen = max(stamps) if stamps else 0.0
    kb_id = str(cluster.get("kb_id") or "")
    terms = ", ".join(missing_terms) if missing_terms else "（未探测）"
    rationale = (
        f"{len(observations)} 次提问、{len(samples)} 个不同问法在「{kb_id}」全部因证据不足被拒；"
        f"召回窗口里没有这些词：{terms}。"
        "这些词是**覆盖度信号**（某个说法没被召回），不等于库里没有对应的说法——"
        "请人工判断该库是否需要补充这批资料，以及补在哪个文档里。"
    )
    return KnowledgeGapTicket(
        cluster_id=int(cluster["id"]),
        kb_id=kb_id,
        question_samples=samples,
        occurrences=len(observations),
        first_seen=first_seen,
        last_seen=last_seen,
        missing_terms=tuple(missing_terms),
        probed_questions=int(probed_questions),
        rationale=rationale,
        created_at=float(time.time() if now is None else now),
    )


__all__ = [
    "DEFAULT_PROBE_LIMIT",
    "DEFAULT_QUESTION_LIMIT",
    "DEFAULT_TERM_LIMIT",
    "KnowledgeGapTicket",
    "aggregate_missing_terms",
    "build_ticket",
    "missing_terms_for",
    "question_samples",
]
