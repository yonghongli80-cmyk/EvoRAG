"""Deterministic-first evidence assembly for one candidate cluster.

The pack follows design section 6's evidence priority: admin-confirmed labels
and other deterministic evidence first, same-KB success traces second,
re-retrieved permission-filtered candidates third. Nothing here calls a model,
opens a network connection, or reads credentials; the reproduction check is a
pure comparison of chunk hashes between the failure time record and one
re-retrieval per question sample. The injected ``retrieve`` callable is the
caller's read-only ACL path (``RAGPipeline._retrieve_visible_candidates`` or an
equivalent), so this module never widens visibility by itself.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rag.evolution.models import EvidencePack, ReproductionResult

# Bounded, deterministic caps: the worker round stays predictable and the pack
# stays small enough for one LLM prompt.
_QUESTION_SAMPLES = 3
_RECALL_CHUNKS = 24
_SUCCESS_TRACES = 3
_TRACE_SCAN_LIMIT = 50
_ANSWER_PREVIEW_CHARS = 200
# Hash lists are prompt input for a reasoning model: each 64-char hash costs
# tokens, so the pack carries a deterministic prefix of each list.
_HASH_PROMPT_LIMIT = 12

# Evidence-priority annotations (design section 6) carried inside the pack so
# the diagnoser ranks evidence without re-deriving the priority.
_LABEL_PRIORITY = "admin_confirmed"
_TRACE_PRIORITY = "system_trace"
# A trace answer is the system's own historical answer, never an admin-confirmed
# one; the annotation keeps that origin explicit inside the pack.
_TRACE_ANSWER_SOURCE = "system_history"
_NO_RETRIEVER_NOTE = "no retriever"
_RETRIEVAL_LEVEL_SUFFIX = "；检索级复现，不含模型判定"


def normalize_question(text: str) -> str:
    """Normalize a question for matching: strip, drop all whitespace, lowercase."""

    return "".join((text or "").split()).lower()


def _text_md5(text: str) -> str:
    """The pipeline's chunk hash: md5 of the chunk text (``rag/pipeline.py``)."""

    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _chunk_text(chunk) -> str:
    """Chunk text of a retrieved candidate; chunk objects and dicts both work."""

    if isinstance(chunk, dict):
        return str(chunk.get("text") or "")
    return str(getattr(chunk, "text", "") or "")


def _hash_tuple(value) -> tuple[str, ...]:
    """Accept the store's decoded JSON column or a still-encoded list; never raise."""

    if not value:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ()
    return tuple(str(item) for item in value)


def _question_samples(members: list[dict]) -> tuple[str, ...]:
    """At most ``_QUESTION_SAMPLES`` distinct member questions, first-seen order.

    Distinctness is computed on the normalized question so punctuation-only
    variants of the same question never crowd out other samples.
    """

    samples: list[str] = []
    seen: set[str] = set()
    for member in members:
        question = str(member.get("question") or "")
        key = normalize_question(question)
        if not key or key in seen:
            continue
        seen.add(key)
        samples.append(question)
        if len(samples) >= _QUESTION_SAMPLES:
            break
    return tuple(samples)


def _member_questions(members: list[dict]) -> tuple[str, ...]:
    """Every member question (not only the capped samples), first-seen order."""

    questions: list[str] = []
    seen: set[str] = set()
    for member in members:
        question = str(member.get("question") or "")
        key = normalize_question(question)
        if not key or key in seen:
            continue
        seen.add(key)
        questions.append(question)
    return tuple(questions)


def _offered_hashes_by_question(members: list[dict]) -> dict[str, tuple[str, ...]]:
    """Failed-time offered chunk hashes per normalized member question."""

    offered: dict[str, tuple[str, ...]] = {}
    for member in members:
        key = normalize_question(str(member.get("question") or ""))
        if not key:
            continue
        offered[key] = offered.get(key, ()) + _hash_tuple(member.get("details_chunk_hashes"))
    return offered


def _load_admin_labels(labels_path) -> tuple[dict, ...]:
    """Load admin-confirmed labels; missing, unreadable, or empty yields ``()``."""

    if labels_path is None:
        return ()
    try:
        raw = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if not isinstance(raw, list):
        return ()
    return tuple(entry for entry in raw if isinstance(entry, dict))


def _matched_admin_labels(questions: tuple[str, ...], labels: tuple[dict, ...]) -> tuple[dict, ...]:
    """Labels whose question matches a member question under normalization.

    Each matched label is copied and annotated with the design-section-6
    priority so the diagnoser ranks evidence without re-deriving it.
    """

    matched: list[dict] = []
    for question in questions:
        key = normalize_question(question)
        if not key:
            continue
        for label in labels:
            if normalize_question(str(label.get("question") or "")) != key:
                continue
            annotated = dict(label)
            annotated["priority"] = _LABEL_PRIORITY
            matched.append(annotated)
    return tuple(matched)


def _success_traces(
    cluster_kb_id: str,
    member_questions: tuple[str, ...],
    tracker,
) -> tuple[dict, ...]:
    """The most recent same-KB, non-refused, answered traces for member questions.

    ``execution_logs`` keeps no knowledge-base column, so same-KB is enforced by
    matching the cluster's own member questions; when a row does carry a
    ``kb_id`` it must equal the cluster's. The answer is the system's historical
    answer (never an admin-confirmed one) and is annotated as such.
    """

    if tracker is None or not member_questions:
        return ()
    member_keys = {normalize_question(question) for question in member_questions}
    traces: list[dict] = []
    for row in tracker.get_recent(_TRACE_SCAN_LIMIT):
        question = str(row.get("question") or "")
        if normalize_question(question) not in member_keys:
            continue
        answer = str(row.get("answer") or "")
        if not answer.strip():
            continue
        route = str(row.get("route") or "")
        if route == "blocked":
            continue
        row_kb_id = row.get("kb_id")
        if row_kb_id and str(row_kb_id) != cluster_kb_id:
            continue
        traces.append({
            "question": question,
            "route": route,
            "chunk_hashes": _hash_tuple(row.get("chunk_hashes")),
            "answer": answer[:_ANSWER_PREVIEW_CHARS],
            "answer_source": _TRACE_ANSWER_SOURCE,
            "priority": _TRACE_PRIORITY,
        })
        if len(traces) >= _SUCCESS_TRACES:
            break
    return tuple(traces)


def _reproduction(
    *,
    questions: tuple[str, ...],
    offered_by_question: dict[str, tuple[str, ...]],
    retrieve,
    kb_id: str,
    recall: set[str],
) -> ReproductionResult:
    """Deterministic retrieval-level reproduction: no model, one re-retrieval per sample.

    A sample reproduces when every chunk hash the failed record offered is still
    offered by the re-retrieval; otherwise the vanished hashes land in
    ``missing_hashes``. A sample without failed hashes, or whose re-retrieval
    fails, cannot confirm reproduction and is treated as inconclusive, so only
    an all-clear verdict is ``reproducible=True``.
    """

    if retrieve is None:
        return ReproductionResult(
            reproducible=False, missing_hashes=(), note=_NO_RETRIEVER_NOTE
        )

    missing: set[str] = set()
    confirmed = 0
    failures = 0
    for question in questions:
        try:
            chunks = retrieve(question, kb_id=kb_id) or []
        except Exception:
            # A broken retriever must never abort the worker round: the sample
            # simply cannot confirm reproduction (design section 9).
            failures += 1
            continue
        hashes = tuple(_text_md5(_chunk_text(chunk)) for chunk in chunks)
        recall.update(hashes[:_RECALL_CHUNKS])
        offered = offered_by_question.get(normalize_question(question), ())
        if not offered:
            continue
        absent = [chunk_hash for chunk_hash in offered if chunk_hash not in hashes]
        if absent:
            missing.update(absent)
        else:
            confirmed += 1

    if missing:
        return ReproductionResult(
            reproducible=False,
            missing_hashes=tuple(sorted(missing)),
            note=(
                f"检索级不可复现：失败时 offered 的块哈希有 {len(missing)} 个已不在重检索结果中"
                f"{_RETRIEVAL_LEVEL_SUFFIX}"
            ),
        )
    if confirmed > 0 and failures == 0:
        return ReproductionResult(
            reproducible=True,
            missing_hashes=(),
            note=f"检索级复现：失败时 offered 的块哈希仍全部在重检索结果中{_RETRIEVAL_LEVEL_SUFFIX}",
        )
    reason = f"{failures} 个样本重检索失败" if failures else "失败记录未提供可核对的块哈希"
    return ReproductionResult(
        reproducible=False,
        missing_hashes=(),
        note=f"检索级复现无法判定：{reason}{_RETRIEVAL_LEVEL_SUFFIX}",
    )


def assemble_evidence(
    cluster: dict,
    *,
    store,
    tracker=None,
    labels_path=None,
    retrieve=None,
) -> EvidencePack:
    """Assemble the deterministic-first evidence pack for one candidate cluster.

    Members come from ``store.list_observations(cluster["id"])``; admin labels
    are matched by normalized question from ``labels_path``; success traces are
    read from the tracker (newest first, capped); ``retrieve`` is the injected
    read-only ACL retrieval used once per question sample for the recall hashes
    and the deterministic reproduction check. The pack never carries
    credentials or unauthorized text.
    """

    members = store.list_observations(cluster["id"])
    kb_id = str(cluster["kb_id"])
    questions = _question_samples(members)
    labels = _load_admin_labels(labels_path)
    recall: set[str] = set()
    reproduction = _reproduction(
        questions=questions,
        offered_by_question=_offered_hashes_by_question(members),
        retrieve=retrieve,
        kb_id=kb_id,
        recall=recall,
    )
    failed_hashes = {
        chunk_hash for member in members for chunk_hash in _hash_tuple(member.get("details_chunk_hashes"))
    }
    # The pack is prompt input for a reasoning model: every 64-character hash is a
    # token cost, so both lists are capped (sorted, so the cap is deterministic and
    # the reproduction check — which runs on the full sets — is unaffected).
    return EvidencePack(
        cluster_id=int(cluster["id"]),
        kb_id=kb_id,
        question_samples=questions,
        failure_stage=str(cluster.get("failure_stage") or ""),
        root_cause_feature=str(cluster.get("root_cause_feature") or ""),
        admin_labels=_matched_admin_labels(questions, labels),
        success_traces=_success_traces(kb_id, _member_questions(members), tracker),
        recall_hashes=tuple(sorted(recall)[:_HASH_PROMPT_LIMIT]),
        failed_hashes=tuple(sorted(failed_hashes)[:_HASH_PROMPT_LIMIT]),
        reproduction=reproduction,
    )


__all__ = ["assemble_evidence", "normalize_question"]
