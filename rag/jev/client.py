"""Fault-isolated TypeSafe adapter.

The pipeline depends on internal contracts only; SDK response classes never leak past this module.
Every failure is normalized into a ``JevBatchResult`` with a failure kind so callers can fall back
to the legacy router/reranker. Logs never include response bodies or credentials.
"""

import logging
import time
from collections.abc import Mapping

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
    PREFLIGHT_QUESTION_SET_VERSION,
    build_passage_questions,
    build_preflight_questions,
)
from rag.resilience import CircuitBreaker

logger = logging.getLogger(__name__)

_PASSAGE_SUFFIXES = (
    "is_relevant",
    "contains_answer_evidence",
    "contradicts_query_premise",
    "contains_prompt_injection",
)

# Exceptions that normalize into a failure kind; anything else is a bug and must surface.
_RECOVERABLE_ERRORS = (TypeSafeError, ValidationError, KeyError, TypeError, AttributeError)


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

    def __init__(self, client: TypeSafeClient | None = None):
        self._client = client
        self._owns_client = client is None
        self._breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

    def preflight(self, state: dict) -> JevBatchResult[PreflightDecision]:
        """Judge intent, complexity, and rewrite need in one batched request."""
        return self._run(
            "preflight",
            state,
            build_preflight_questions(),
            lambda response, duration_ms: self._map_preflight(response, duration_ms),
        )

    def judge_passages(
        self, state: dict, chunk_ids: list[str]
    ) -> JevBatchResult[tuple[PassageJudgment, ...]]:
        """Judge ACL-visible candidate passages in one batched request."""
        if not chunk_ids:
            return JevBatchResult(available=False, failure_kind="invalid_response", duration_ms=0.0)
        return self._run(
            "passages",
            state,
            build_passage_questions(chunk_ids),
            lambda response, duration_ms: self._map_passages(response, chunk_ids, duration_ms),
        )

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

    def _map_preflight(self, response, duration_ms: float) -> PreflightDecision:
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
            question_set_version=PREFLIGHT_QUESTION_SET_VERSION,
            duration_ms=duration_ms,
        )

    def _map_passages(
        self, response, chunk_ids: list[str], duration_ms: float
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


__all__ = ["JevClient"]
