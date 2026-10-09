"""Deterministic-first evidence assembly for one member observation.

Phase 4 revises the granularity (``docs/evolution-diagnosis.md`` §5): the
clustering key (kb × stage × root-cause feature) gathers distinct root causes
into one cluster, so a cluster-level single draft can never match a mixed label
set — for example a ``kb_hr`` cluster that holds both "candidate squeezed out of
the pool" and "no such document in the library". The pack therefore covers a
single failure trajectory (design sections 5/6/9: a failure trajectory is the
observation-level entity; the cluster is only the admission grouping), and the
caller passes ``(observation, cluster)`` instead of a cluster row alone.

The pack follows design section 6's evidence priority: admin-confirmed labels
and other deterministic evidence first, same-KB success traces second,
re-retrieved permission-filtered candidates third. Nothing here calls a model,
opens a network connection, or reads credentials; the reproduction check is a
pure comparison of chunk hashes between the failure-time record and one
re-retrieval per question. The injected ``retrieve`` callable is the caller's
read-only ACL path (``RAGPipeline._retrieve_visible_candidates`` or an
equivalent), so this module never widens visibility by itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from rag.evolution.models import EvidencePack, ReproductionResult

# Bounded, deterministic caps: the worker round stays predictable and the pack
# stays small enough for one LLM prompt.
_RECALL_CHUNKS = 24
_SUCCESS_TRACES = 3
_TRACE_SCAN_LIMIT = 50
_ANSWER_PREVIEW_CHARS = 200
# Hash lists are prompt input for a reasoning model: each 64-char hash costs
# tokens, so the pack carries a deterministic prefix of each list.
_HASH_PROMPT_LIMIT = 12
# Chunk previews let the model judge "the answer is not in the retrieved text"
# versus "the needed chunk was never retrieved" — hash-only evidence cannot.
# The cap must cover enough of the re-retrieval for a low-ranked but relevant
# chunk to stay visible; eight previews hid the third-party accessory table and
# produced a wrong content-gap verdict in the dev set.
_PREVIEW_CHARS = 160
_PREVIEW_LIMIT = 24

# Question-term coverage is the deterministic answer to "is the question's own
# wording anywhere in the recall set, and how deep". Two characters is the
# shortest term worth reporting (a single character matches almost anything), and
# the term list is capped because every term is prompt input.
_COVERAGE_TERMS = 8
_MIN_TERM_CHARS = 2

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


_CJK_RANGE = "\u4e00-\u9fff"
_ASCII_TERM = re.compile(r"[0-9a-z_]+")
_CJK_RUN = re.compile(f"[{_CJK_RANGE}]+")


def _question_terms(question: str) -> tuple[str, ...]:
    """The deterministic terms a question contributes to the coverage signal.

    ASCII/digit words are taken whole; CJK text contributes character bigrams.
    Bigrams are used instead of a word segmenter so the signal stays
    deterministic, dependency-free and cheap, and the approximation is in the
    safe direction: a reported term occurs in the text verbatim, so a hit is
    always real. Terms keep their first-appearance order and are capped.
    """
    terms: list[str] = []
    for word in _ASCII_TERM.findall(question.lower()):
        if len(word) >= _MIN_TERM_CHARS:
            terms.append(word)
    for run in _CJK_RUN.findall(question):
        for index in range(len(run) - 1):
            terms.append(run[index : index + 2])
    ordered: list[str] = []
    for term in terms:
        if term not in ordered:
            ordered.append(term)
    return tuple(ordered[:_COVERAGE_TERMS])


def _term_coverage(terms: Sequence[str], texts: Sequence[str]) -> tuple[dict, ...]:
    """Per term: how many recall chunks contain it, and the earliest rank that does.

    ``chunks=0, best_rank=0`` is reported as-is — a term the recall set does not
    contain is evidence about the corpus, not a gap in the pack.
    """
    records: list[dict] = []
    for term in terms:
        hits = 0
        best_rank = 0
        for rank, text in enumerate(texts, start=1):
            if term in text:
                hits += 1
                if best_rank == 0:
                    best_rank = rank
        records.append({"term": term, "chunks": hits, "best_rank": best_rank})
    return tuple(records)


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
    previews: list[dict] | None = None,
    recall_texts: list[str] | None = None,
) -> ReproductionResult:
    """Deterministic retrieval-level reproduction: no model, one re-retrieval per sample.

    A sample reproduces when every chunk hash the failed record offered is still
    offered by the re-retrieval; otherwise the vanished hashes land in
    ``missing_hashes``. A sample without failed hashes, or whose re-retrieval
    fails, cannot confirm reproduction and is treated as inconclusive, so only
    an all-clear verdict is ``reproducible=True``.

    When ``previews`` is a list, each re-retrieved chunk contributes one bounded
    preview (document, hash, leading text). The previews describe the *current*
    retrieval result — for versioned corpora they are not the failure-time text,
    and the diagnoser renders them under that label.

    ``recall_texts`` collects the *full* text of the same bounded recall window,
    because the coverage signal has to look past the preview's character cap: a
    term that only occurs deep inside a chunk still counts as covered.
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
        for chunk in chunks[:_RECALL_CHUNKS]:
            chunk_hash = _text_md5(_chunk_text(chunk))
            recall.add(chunk_hash)
            if recall_texts is not None:
                recall_texts.append(_chunk_text(chunk))
            if previews is not None and len(previews) < _PREVIEW_LIMIT:
                previews.append({
                    "doc": getattr(chunk, "doc_name", ""),
                    "hash": chunk_hash,
                    "text": _chunk_text(chunk)[:_PREVIEW_CHARS],
                })
        hashes = tuple(_text_md5(_chunk_text(chunk)) for chunk in chunks)
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
    observation: dict,
    cluster: dict,
    *,
    store,
    tracker=None,
    labels_path=None,
    retrieve=None,
) -> EvidencePack:
    """Assemble the deterministic-first evidence pack for one member observation.

    ``observation`` is the stored failure-observation row; ``cluster`` is the
    admitting cluster row, consulted only for the signals the observation does
    not overwrite (its kb_id, and the failure stage / root-cause feature the
    observation left empty). ``question_samples`` is exactly this observation's
    question; ``failed_hashes`` are exactly its own offered chunk hashes (a
    sibling member's root cause never leaks in); the admin labels matched by
    normalized question, the same-KB success traces and the one re-retrieval
    behind the deterministic reproduction check are all scoped to it. ``retrieve``
    is the injected read-only ACL retrieval, used once for the recall hashes and
    the reproduction verdict. ``store`` is kept for the documented caller wiring;
    the observation and cluster rows are read directly, so no member lookup is
    needed here. The pack never carries credentials or unauthorized text.
    """

    question = str(observation.get("question") or "")
    questions = (question,) if question else ()
    kb_id = str(observation.get("kb_id") or cluster.get("kb_id") or "")
    labels = _load_admin_labels(labels_path)

    offered_key = normalize_question(question)
    offered_by_question = (
        {offered_key: _hash_tuple(observation.get("details_chunk_hashes"))} if offered_key else {}
    )

    recall: set[str] = set()
    previews: list[dict] = []
    recall_texts: list[str] = []
    reproduction = _reproduction(
        questions=questions,
        offered_by_question=offered_by_question,
        retrieve=retrieve,
        kb_id=kb_id,
        recall=recall,
        previews=previews,
        recall_texts=recall_texts,
    )
    failed_hashes = _hash_tuple(observation.get("details_chunk_hashes"))
    matched_labels = _matched_admin_labels(questions, labels)
    label_hits = _label_evidence(matched_labels, recall_texts, previews)
    # The pack is prompt input for a reasoning model: every 64-character hash is a
    # token cost, so both lists are capped (sorted, so the cap is deterministic and
    # the reproduction check — which runs on the full sets — is unaffected).
    return EvidencePack(
        cluster_id=int(cluster["id"]),
        kb_id=kb_id,
        question_samples=questions,
        failure_stage=str(observation.get("stage") or cluster.get("failure_stage") or ""),
        root_cause_feature=str(
            observation.get("root_cause_feature") or cluster.get("root_cause_feature") or ""
        ),
        admin_labels=matched_labels,
        success_traces=_success_traces(kb_id, questions, tracker),
        recall_hashes=tuple(sorted(recall)[:_HASH_PROMPT_LIMIT]),
        failed_hashes=tuple(sorted(failed_hashes)[:_HASH_PROMPT_LIMIT]),
        recall_previews=tuple(previews),
        # Coverage is only evidence when there is a recall window to measure: with
        # no re-retrieval (or an empty one) "0 hits" would claim the corpus lacks
        # the term when nothing was measured at all.
        question_term_coverage=(
            _term_coverage(_question_terms(question), recall_texts) if recall_texts else ()
        ),
        passage_judging=_passage_judging(observation),
        failure_route=str(observation.get("route") or ""),
        exception_class=str(observation.get("details_exception_class") or ""),
        exception_frame=str(observation.get("details_exception_frame") or ""),
        label_evidence=label_hits,
        label_evidence_absent=_label_evidence_absent(matched_labels, recall_texts),
        reproduction=reproduction,
    )


#: 判定层写进 decision_metadata 的统计量；缺失即"该失败早于统计量上线"。
_JUDGING_KEYS = (
    "passage_judged",
    "passage_relevant",
    "passage_evidence",
    "passage_contradicted",
    "passage_injected",
)


def _passage_judging(observation: dict) -> tuple[tuple[str, int], ...]:
    """What the passage judge reported for the failed request, straight from its metadata.

    Read from the observation rather than recomputed: re-judging now would answer a
    different question (what the current question set thinks) than the one the diagnosis
    asks (what the failing request's judge saw).
    """
    metadata = observation.get("details_decision_metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return ()
    if not isinstance(metadata, Mapping):
        return ()
    counts = []
    for key in _JUDGING_KEYS:
        value = metadata.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        counts.append((key, int(value)))
    return tuple(counts)


#: 从管理员答案文本里抽型号串：必须同时含字母与数字（LH716-01、HA036、EN-EL18d、LP-E17…），
#: 长度 ≥4。纯中文短语与纯数字都不取——前者无法精确匹配，后者噪声太大。
_ANSWER_TOKEN = re.compile(r"[A-Za-z]{1,6}[-]?\d[\w.\-]{0,12}")
_ANSWER_TOKEN_LIMIT = 6


def _answer_tokens(text: str) -> tuple[str, ...]:
    """The part-number-shaped tokens an admin answer states, deduplicated and capped."""
    tokens: list[str] = []
    for match in _ANSWER_TOKEN.finditer(text or ""):
        token = match.group(0)
        if len(token) < 4 or not any(char.isalpha() for char in token) or not any(
            char.isdigit() for char in token
        ):
            continue
        if token not in tokens:
            tokens.append(token)
        if len(tokens) >= _ANSWER_TOKEN_LIMIT:
            break
    return tuple(tokens)


def _label_evidence(
    labels: tuple[dict, ...], recall_texts: list[str], previews: list[dict]
) -> tuple[dict, ...]:
    """Do the admin answer's own part numbers appear in the recall window?

    The one deterministic way to tell "the content is there and was not adopted"
    (``ranking_error``) from "the content is not there" (``undetermined``): look for the
    exact answer string the administrator wrote, in the same recall window the coverage
    signal measures. Words matching is not enough — this looks for the answer itself.
    """
    hits: list[dict] = []
    for label in labels:
        for token in _answer_tokens(str(label.get("expected_answer") or "")):
            matched_ranks = [
                index
                for index, text in enumerate(recall_texts)
                if token.lower() in (text or "").lower()
            ]
            if not matched_ranks:
                continue
            first = matched_ranks[0]
            hits.append(
                {
                    "question": str(label.get("question") or ""),
                    "token": token,
                    "chunks": len(matched_ranks),
                    "first_rank": first,
                    "hash": previews[first]["hash"] if first < len(previews) else "",
                }
            )
    return tuple(hits)


def _label_evidence_absent(labels: tuple[dict, ...], recall_texts: list[str]) -> tuple[str, ...]:
    """Which of the admin answer's part numbers the recall window never contains.

    The same deterministic check as :func:`_label_evidence`, read the other way round and from
    the same source of truth (the recall window's full text). The model can see what was
    retrieved and never what is missing, so without this it cannot tell "the answer was never
    recalled" (``recall_miss``) from "the answer came back and the judge ignored it" — the two
    it alternates between on identical evidence.
    """

    absent: list[str] = []
    for label in labels:
        for token in _answer_tokens(str(label.get("expected_answer") or "")):
            if token in absent:
                continue
            if any(token.lower() in (text or "").lower() for text in recall_texts):
                continue
            absent.append(token)
    return tuple(absent)


__all__ = ["assemble_evidence", "normalize_question"]
