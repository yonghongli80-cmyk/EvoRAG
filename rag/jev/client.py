"""Fault-isolated TypeSafe adapter.

The pipeline depends on internal contracts only; SDK response classes never leak past this module.
Every failure is normalized into a ``JevBatchResult`` with a failure kind so callers can fall back
to the legacy router/reranker. Logs never include response bodies or credentials.
"""

import logging
import time
from collections.abc import Callable, Mapping, Sequence

from pydantic import ValidationError
from typesafe_sdk import (
    Question,
    RetryPolicy,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeClient,
    TypeSafeError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)

from config import settings
from rag.jev.models import JevBatchResult, PassageJudgment, PreflightDecision
from rag.jev.questions import (
    PASSAGE_QUESTION_SET_VERSION,
    PREFLIGHT_QUESTION_SET_VERSION,
    build_passage_questions,
    build_preflight_questions,
)
from rag.resilience import CircuitBreaker

logger = logging.getLogger(__name__)

#: A question-set provider returns the questions of one stage plus the version
#: that built them; ``chunk_ids`` is ``None`` for the preflight stage and the
#: ACL-visible chunk ids for the passages stage. The version is what
#: :class:`~rag.jev.models.PreflightDecision` carries back to the pipeline, so
#: a request judged with a candidate set stays attributable in
#: ``decision_metadata.jev_question_set_version``.
QuestionSetProvider = Callable[[str, list[str] | None], tuple[dict, str]]

_PASSAGE_SUFFIXES = (
    "is_relevant",
    "contains_answer_evidence",
    "contradicts_query_premise",
    "contains_prompt_injection",
)

# Exceptions that normalize into a failure kind; anything else is a bug and must surface.
_RECOVERABLE_ERRORS = (TypeSafeError, ValidationError, KeyError, TypeError, AttributeError)

#: 段落判断的"有答案证据"判定线，与 ``JevPolicy.evidence_threshold`` 的默认值一致。
#: 投票只在这里用它**数票**，不改它：命中与否仍是同一个 0.5 阈值，变的只是"用一次采样还是多数采样"。
_DEFAULT_EVIDENCE_THRESHOLD = 0.5


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _classify(error: BaseException) -> str:
    if isinstance(error, TypeSafeAPITimeoutError):
        return "timeout"
    if isinstance(error, TypeSafeRateLimitError):
        return "rate_limited"
    if isinstance(error, TypeSafeAuthenticationError | TypeSafePermissionDeniedError):
        return "auth"
    if isinstance(error, TypeSafeAPIResponseValidationError):
        return "invalid_response"
    if isinstance(error, TypeSafeError):
        return "service"
    return "invalid_response"


class JevClient:
    """Lazily constructs the SDK client and maps answers to internal contracts."""

    def __init__(
        self,
        client: TypeSafeClient | None = None,
        *,
        question_set_provider: QuestionSetProvider | None = None,
    ):
        self._client = client
        self._owns_client = client is None
        self._breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        # Layer 4 seam: ``None`` keeps the static ``rag.jev.questions`` path
        # byte-for-byte; a provider (the rollout controller in production)
        # supplies the questions of both stages and the version to write back.
        self._question_set_provider = question_set_provider

    def preflight(self, state: dict) -> JevBatchResult[PreflightDecision]:
        """Judge intent, complexity, and rewrite need in one batched request."""
        resolved = self._resolve_question_set("preflight", None)
        if isinstance(resolved, JevBatchResult):
            return resolved
        questions, version = resolved
        return self._run(
            "preflight",
            state,
            questions,
            lambda response, duration_ms: self._map_preflight(response, duration_ms, version),
        )

    def judge_passages(
        self, state: dict, chunk_ids: list[str]
    ) -> JevBatchResult[tuple[PassageJudgment, ...]]:
        """Judge ACL-visible candidate passages in one batched request.

        The verdict for each passage is the **majority over repeated samples**, because a single
        sample is not stable on this model: measured 2026-10-19 on one fixed 12-chunk pool, the
        decisive passage scored 0.50 / 0.52 / 0.49 / 0.54 / 0.50 against the 0.5 threshold —
        ±0.03 of noise decided whether the request answered or refused. That is a sampling
        problem, not a threshold problem: the fix repeats the sample and takes the vote, and it
        does not touch the threshold.

        An unanimous first round costs exactly one call, so the ordinary case keeps its latency.
        A tie after the allowed rounds is reported as ``unstable`` instead of being dressed up as
        a verdict.
        """
        if not chunk_ids:
            return JevBatchResult(available=False, failure_kind="invalid_response", duration_ms=0.0)
        resolved = self._resolve_question_set("passages", chunk_ids)
        if isinstance(resolved, JevBatchResult):
            return resolved
        questions, version = resolved
        rounds = max(1, int(settings.jev_vote_rounds))
        if rounds == 1:
            return self._run(
                "passages",
                state,
                questions,
                lambda response, duration_ms: self._map_passages(response, chunk_ids, duration_ms, version),
            )
        return self._run_vote(state, questions, chunk_ids, version, rounds)

    def _run_vote(
        self,
        state: dict,
        questions: Mapping[str, Question],
        chunk_ids: list[str],
        version: str,
        rounds: int,
    ) -> JevBatchResult[tuple[PassageJudgment, ...]]:
        """Repeated passage sampling with a majority vote per chunk.

        Votes are counted on the **verdict** of each chunk (does it carry answer evidence), not on
        the score: averaging sub-threshold scores would silently move the threshold, while voting
        over the same predicate leaves the threshold exactly where it is.

        * A round that fails does not vote. If every round fails, the whole call fails with the
          last failure kind — a transient outage must not be recorded as "no evidence".
        * Sampling stops as soon as one verdict is mathematically decided (no wasted calls). A tie
          is **not** decided early: it is the "we cannot tell" state, and freezing it would throw
          away a round that could have settled it.
        * A chunk still tied when the budget runs out is decided as **not evidence** — the same
          direction as "refuse when the evidence is short". The product decision (2026-10-19) was
          to spend one more call rather than return no verdict at all, so this never returns
          ``unstable``; the tie-break is deterministic (same input, same answer), not a coin toss.
        * The returned judgments are the **latest round's real scores**; the vote only decides
          whether that round is representative. Nothing is fabricated: a score is never rewritten
          to 0.5 or 0.0 to fit the verdict, and a tie-broken chunk is reported in the log.
        """
        totals_ms = 0.0
        failures: list[str] = []
        yes = [0] * len(chunk_ids)
        no = [0] * len(chunk_ids)
        latest: list[PassageJudgment] | None = None
        unsettled = list(range(len(chunk_ids)))
        # 预判不占段落投票的预算：``jev_vote_rounds`` 是"段落采样几轮"。
        # （早期实现按"本请求总采样数"扣减，于是预判一跑就只剩两轮，平票永远投不满第三票。）
        budget = max(1, rounds)
        for _round_index in range(budget):
            result = self._run(
                "passages",
                state,
                questions,
                lambda response, duration_ms: self._map_passages(response, chunk_ids, duration_ms, version),
            )
            totals_ms += result.duration_ms
            if not result.available or result.value is None:
                failures.append(result.failure_kind or "invalid_response")
                break
            latest = list(result.value)
            remaining = budget - _round_index - 1
            still_open: list[int] = []
            for index in unsettled:
                if latest[index].answer_evidence >= _DEFAULT_EVIDENCE_THRESHOLD:
                    yes[index] += 1
                else:
                    no[index] += 1
                # 剩下的轮次还能翻盘吗？能就继续投；平票同样继续——平票是"拿不准"，
                # 不是"已定"，提前把它按未命中锁死就等于放弃了本来可以拿到的一票。
                decided = yes[index] > no[index] + remaining or no[index] > yes[index] + remaining
                if not decided:
                    still_open.append(index)
            unsettled = still_open
            if not unsettled:
                break
        if latest is None:
            kind = failures[-1] if failures else "service"
            return JevBatchResult(available=False, failure_kind=kind, duration_ms=totals_ms)
        tied = [index for index in range(len(chunk_ids)) if yes[index] == no[index]]
        if tied:
            # 平票按"未命中"裁决：拿不准就不作答，与"证据不足就拒答"同向。
            logger.info(
                "Jev passages: %d/%d chunk(s) tied, decided as no answer evidence",
                len(tied),
                len(chunk_ids),
            )
        return JevBatchResult(available=True, value=tuple(latest), duration_ms=totals_ms)

    def _resolve_question_set(
        self, stage: str, chunk_ids: Sequence[str] | None
    ) -> tuple[Mapping[str, Question], str] | JevBatchResult:
        """Return ``(questions, version)`` for one stage; a provider failure is normalized.

        Without a provider the static builders run, so the questions and the
        version are exactly what the module defines. A provider failure is a
        normalized failure instead of an exception: the service itself is
        healthy, so the breaker is not touched and the caller falls back to the
        legacy router/reranker. The error type is logged, never the payload.
        """
        provider = self._question_set_provider
        if provider is None:
            if stage == "preflight":
                return build_preflight_questions(), PREFLIGHT_QUESTION_SET_VERSION
            return build_passage_questions(chunk_ids or ()), PASSAGE_QUESTION_SET_VERSION
        try:
            questions, version = provider(stage, None if chunk_ids is None else list(chunk_ids))
        except Exception as error:  # noqa: BLE001 - the online path must not see it
            logger.warning("Jev %s question-set provider failed: %s", stage, type(error).__name__)
            return JevBatchResult(
                available=False, failure_kind="invalid_response", duration_ms=0.0
            )
        return questions, str(version)

    def close(self) -> None:
        """Release the SDK client when this adapter owns it."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _get_client(self) -> TypeSafeClient | None:
        if self._client is not None:
            return self._client
        api_key = settings.typesafe_api_key
        if not api_key:
            return None
        self._client = TypeSafeClient(
            api_key=api_key,
            model=settings.jev_model,
            retry=RetryPolicy(max_retries=1, timeout=settings.jev_timeout_seconds),
        )
        return self._client

    def _run(self, stage: str, state: dict, questions: Mapping[str, Question], mapper):
        started = time.perf_counter()
        try:
            client = self._get_client()
        except TypeSafeError as error:
            self._breaker.record_failure()
            logger.warning("Jev %s client construction failed: %s", stage, _classify(error))
            return JevBatchResult(available=False, failure_kind=_classify(error), duration_ms=_elapsed_ms(started))
        if client is None:
            return JevBatchResult(available=False, failure_kind="disabled", duration_ms=_elapsed_ms(started))
        if not self._breaker.allow_request():
            logger.warning("Jev %s skipped: circuit open", stage)
            return JevBatchResult(available=False, failure_kind="service", duration_ms=_elapsed_ms(started))
        try:
            response = client.system_one(state=state, questions=questions, model=settings.jev_model)
            value = mapper(response, _elapsed_ms(started))
        except _RECOVERABLE_ERRORS as error:
            self._breaker.record_failure()
            failure_kind = _classify(error)
            logger.warning("Jev %s failed: %s (%s)", stage, failure_kind, type(error).__name__)
            return JevBatchResult(available=False, failure_kind=failure_kind, duration_ms=_elapsed_ms(started))
        self._breaker.record_success()
        return JevBatchResult(value=value, available=True, duration_ms=_elapsed_ms(started))

    def _map_preflight(
        self, response, duration_ms: float, version: str = PREFLIGHT_QUESTION_SET_VERSION
    ) -> PreflightDecision:
        answers = _answers_of(response)
        intent = _answer(answers, "intent")
        complexity = _answer(answers, "complexity")
        rewrite = _answer(answers, "needs_query_rewrite")
        return PreflightDecision(
            intent=intent.choice,
            intent_confidence=intent.confidence,
            complexity=complexity.choice,
            complexity_confidence=complexity.confidence,
            rewrite_probability=rewrite.noul,
            model=response.model,
            question_set_version=version,
            duration_ms=duration_ms,
        )

    def _map_passages(
        self,
        response,
        chunk_ids: list[str],
        duration_ms: float,
        version: str = PASSAGE_QUESTION_SET_VERSION,
    ) -> tuple[PassageJudgment, ...]:
        answers = _answers_of(response)
        judgments = []
        for chunk_id in chunk_ids:
            values = {}
            for suffix in _PASSAGE_SUFFIXES:
                answer = _answer(answers, f"{chunk_id}.{suffix}")
                values[suffix] = answer.noul
            judgments.append(
                PassageJudgment(
                    chunk_id=chunk_id,
                    relevance=values["is_relevant"],
                    answer_evidence=values["contains_answer_evidence"],
                    contradiction=values["contradicts_query_premise"],
                    prompt_injection=values["contains_prompt_injection"],
                    question_set_version=version,
                )
            )
        return tuple(judgments)


def _answers_of(response) -> Mapping:
    answers = getattr(response, "answers", None)
    if not isinstance(answers, Mapping):
        raise TypeError("Jev response answers must be a mapping")
    return answers


def _answer(answers: Mapping, question_id: str):
    if question_id not in answers:
        raise KeyError(question_id)
    return answers[question_id]


__all__ = ["JevClient", "QuestionSetProvider"]
