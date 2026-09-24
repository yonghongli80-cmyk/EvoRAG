"""Layers 4-5: the percentage rollout controller for question-set candidates.

One candidate question set may serve live traffic only after an audited
administrator canary action, and it must be removable in one step. This module
is the whole mechanism:

* **In-memory single live rollout.** ``activate`` registers the candidate
  question set in :mod:`rag.jev.questions` (a data registration next to the
  default versions, never a replacement) and records the live percentage;
  ``deactivate`` unregisters it and zeroes the percentage. A percentage of
  zero registers nothing, which is the default: with nothing registered the
  provider returns the current question set and the online path is bit-for-bit
  the old one.
* **Per-request bucketing.** ``should_use_candidate`` hashes the request key
  with sha256 (stable across processes, unlike the salted builtin ``hash`` on
  ``str``, so a request keeps its bucket across restarts and stays attributable
  in logs) and compares ``bucket < percent``. Deterministic, so the same
  request always lands in the same bucket.
* **The provider.** ``provide`` is the callable injected into
  :class:`~rag.jev.client.JevClient`. It reads the request key the pipeline
  publishes through :meth:`CandidateRollout.request_key`; without a published
  key (a deployment that never wired the pipeline) it falls back to the current
  set, so a missing wiring can never route candidate traffic.
* **Persistence without DDL.** The live percentage is written to
  ``evolution_candidate_audit`` as a machine-readable note
  (``canary_percent=N``) inside the canary/rollback audit rows that the store
  already writes for these actions. :meth:`CandidateRollout.restore` replays
  that trail and reconciles the in-memory state with it, so a restarted API
  process resumes (or drops) exactly what the administrator persisted. No new
  column, no ``gate_log`` pollution — a percentage is not a gate result — and
  rollback depends on the stored trail alone.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from rag.evolution.models import EvolutionStatus
from rag.jev.questions import (
    PASSAGE_QUESTION_SET_VERSION,
    PREFLIGHT_QUESTION_SET_VERSION,
    build_passage_questions,
    build_preflight_questions,
    build_with_overlay,
    register_candidate_question_set,
    unregister_candidate_question_set,
)

# The audit note carries the percentage in a machine-readable form so
# ``restore`` can rebuild live state from the trail alone.
PERCENT_NOTE_KEY = "canary_percent"
_PERCENT_NOTE = re.compile(rf"{PERCENT_NOTE_KEY}=(\d+)")

# A status whose candidate still serves traffic (``stable`` once an admin
# promotes it; nothing promotes candidates in this phase, but a stored row in
# that status must not silently stop serving).
_SERVING_STATUSES = (EvolutionStatus.CANARY.value, EvolutionStatus.STABLE.value)

# ``restore`` scans the candidates of the serving statuses; the limit only
# bounds one rebuild pass, never the live set.
_RESTORE_SCAN_LIMIT = 1000


def candidate_question_set_version(base_version: str, candidate_id: int) -> str:
    """The version a candidate question set is served under.

    It keeps the base version visible (``preflight-v1+candidate-7``), so a
    trace's ``jev_question_set_version`` names both the live set the candidate
    was generated from and the candidate that served the request.
    """
    return f"{base_version}+candidate-{candidate_id}"


def percent_note(percent: int) -> str:
    """The audit note of a canary action: human text plus the replayable token."""
    return f"灰度 {percent}%｜{PERCENT_NOTE_KEY}={percent}"


def _percent_from_note(note: str) -> int | None:
    match = _PERCENT_NOTE.search(note)
    return None if match is None else int(match.group(1))


def bucket(request_key: str) -> int:
    """The stable 0-99 bucket of one request key."""
    digest = hashlib.sha256(request_key.encode("utf-8")).hexdigest()
    return int(digest, 16) % 100


def _base_question_set(stage: str, chunk_ids) -> tuple[dict, str]:
    """The current question set of one stage: what a 0% rollout serves."""
    if stage == "passages":
        return build_passage_questions(chunk_ids or ()), PASSAGE_QUESTION_SET_VERSION
    return build_preflight_questions(), PREFLIGHT_QUESTION_SET_VERSION


@dataclass(frozen=True)
class _Rollout:
    """One registered candidate: its payload, version and live percentage."""

    candidate_id: int
    target: str
    payload: dict
    base_version: str
    version: str
    percent: int


@dataclass(frozen=True)
class RolloutSnapshot:
    """What an administrator (or a log) reads about the live rollout."""

    candidate_id: int
    target: str
    version: str
    percent: int


class CandidateRollout:
    """The layer-4/5 controller: one live canary, bucketed per request."""

    def __init__(self) -> None:
        self._rollouts: dict[int, _Rollout] = {}
        self._lock = threading.Lock()
        # The request key of the request in flight, published by
        # :meth:`request_key`. A ``ContextVar`` (not an attribute) keeps two
        # concurrent requests from overwriting each other's key: each thread
        # and each task runs in its own context. Empty means "no per-request
        # identity", and an empty key never serves a candidate.
        self._request_key: ContextVar[str] = ContextVar(
            "jev_candidate_request_key", default=""
        )

    # ------------------------------------------------------------------
    # Administrator actions (the API endpoints call these).
    # ------------------------------------------------------------------

    def activate(
        self,
        candidate_id: int,
        target: str,
        payload: dict,
        base_version: str,
        percent: int,
    ) -> str:
        """Register one candidate at ``percent`` and return the version it serves.

        ``percent`` zero registers nothing: the candidate is prepared but no
        request is routed to it, which is the default online behaviour. An
        unknown target is a programming error and raises ``ValueError``.
        """
        if target not in ("preflight", "passages"):
            raise ValueError(f"unknown candidate target: {target!r}")
        if percent < 0 or percent > 100:
            raise ValueError(f"canary percent must be 0..100, got {percent}")
        version = candidate_question_set_version(base_version, int(candidate_id))
        entry = _Rollout(
            candidate_id=int(candidate_id),
            target=target,
            payload=dict(payload),
            base_version=base_version,
            version=version,
            percent=int(percent),
        )
        with self._lock:
            if percent > 0:
                register_candidate_question_set(
                    target,
                    version,
                    lambda chunk_ids=None: build_with_overlay(target, payload, chunk_ids),
                )
                self._rollouts[int(candidate_id)] = entry
            else:
                # Zero percent means "not registered": drop the data
                # registration and keep no live entry.
                unregister_candidate_question_set(target, version)
                self._rollouts.pop(int(candidate_id), None)
        return version

    def deactivate(self, candidate_id: int) -> None:
        """Unregister one candidate and zero its percentage (idempotent)."""
        with self._lock:
            rollout = self._rollouts.pop(int(candidate_id), None)
        if rollout is not None:
            unregister_candidate_question_set(rollout.target, rollout.version)

    def percent_for(self, candidate_id: int) -> int:
        """The live percentage of one candidate (0 when it serves nothing)."""
        with self._lock:
            rollout = self._rollouts.get(int(candidate_id))
        return 0 if rollout is None else rollout.percent

    def snapshot(self) -> list[RolloutSnapshot]:
        """Every live registration, newest candidate first (read-only)."""
        with self._lock:
            rollouts = list(self._rollouts.values())
        return sorted(
            (
                RolloutSnapshot(
                    candidate_id=rollout.candidate_id,
                    target=rollout.target,
                    version=rollout.version,
                    percent=rollout.percent,
                )
                for rollout in rollouts
            ),
            key=lambda entry: entry.candidate_id,
            reverse=True,
        )

    # ------------------------------------------------------------------
    # The per-request decision and the provider the Jev client calls.
    # ------------------------------------------------------------------

    @contextmanager
    def request_key(self, key: str) -> Iterator[None]:
        """Publish the request key for the Jev decisions inside this block.

        The pipeline wraps one request's Jev calls in this scope, so the
        preflight and the passages judge of the same request land in the same
        bucket. The key never leaves the process.
        """
        token = self._request_key.set(key)
        try:
            yield
        finally:
            self._request_key.reset(token)

    def current_request_key(self) -> str:
        return self._request_key.get()

    def should_use_candidate(self, request_key: str) -> bool:
        """True when ``request_key`` lands in the bucket of the live canary.

        ``bucket(key) < percent`` is deterministic, so one request always gets
        the same answer; an empty key (no published identity) never does.
        """
        if not request_key:
            return False
        with self._lock:
            live = self._live_rollout()
        return live is not None and bucket(request_key) < live.percent

    def provide(self, stage: str, chunk_ids=None) -> tuple[dict, str]:
        """The question set of one stage for the request in flight.

        Returns ``(questions, version)``: the candidate overlay plus its
        version when this request is inside the bucket, the current question
        set plus its default version otherwise.
        """
        with self._lock:
            live = self._live_rollout()
        key = self._request_key.get()
        if live is None or live.target != stage or not key or bucket(key) >= live.percent:
            return _base_question_set(stage, chunk_ids)
        return build_with_overlay(stage, live.payload, chunk_ids), live.version

    # ------------------------------------------------------------------
    # Recovery from the persisted trail.
    # ------------------------------------------------------------------

    def restore(self, store) -> None:
        """Reconcile live state with the persisted canary trail (idempotent).

        Every candidate whose status still allows it to serve traffic is
        re-registered at the percentage its audit trail records; everything
        else in memory is dropped. A process that restarted while a canary was
        live therefore resumes it, and one whose trail was rolled back drops
        it. No audit row is written — this only rebuilds memory and the data
        registrations the provider serves from.
        """
        recovered: dict[int, _Rollout] = {}
        for status in _SERVING_STATUSES:
            for row in store.list_candidates(status=status, limit=_RESTORE_SCAN_LIMIT):
                percent = self._persisted_percent(store, int(row["id"]))
                payload = row.get("payload")
                rollback_ref = row.get("rollback_ref")
                if percent <= 0 or not isinstance(payload, Mapping) or not isinstance(rollback_ref, Mapping):
                    # A row that cannot be served (a hand-edited payload or a
                    # rolled-back trail) simply stays off the live path.
                    continue
                candidate_id = int(row["id"])
                base_version = str(rollback_ref.get("question_set_version") or "")
                target = str(row["target"])
                if not base_version or target not in ("preflight", "passages"):
                    continue
                recovered[candidate_id] = _Rollout(
                    candidate_id=candidate_id,
                    target=target,
                    payload=dict(payload),
                    base_version=base_version,
                    version=candidate_question_set_version(base_version, candidate_id),
                    percent=percent,
                )
        with self._lock:
            for rollout in self._rollouts.values():
                if rollout.candidate_id not in recovered:
                    unregister_candidate_question_set(rollout.target, rollout.version)
            self._rollouts = recovered
        for rollout in recovered.values():
            register_candidate_question_set(
                rollout.target,
                rollout.version,
                lambda chunk_ids=None, target=rollout.target, payload=rollout.payload: (
                    build_with_overlay(target, payload, chunk_ids)
                ),
            )

    @staticmethod
    def _persisted_percent(store, candidate_id: int) -> int:
        """Replay one candidate's audit trail for its last canary percentage."""
        percent = 0
        for row in store.list_audit(candidate_id):
            action = str(row.get("action") or "")
            if action == "rollback":
                percent = 0
                continue
            if action != "canary":
                continue
            parsed = _percent_from_note(str(row.get("note") or ""))
            if parsed is not None:
                percent = parsed
        return percent

    def _live_rollout(self) -> _Rollout | None:
        """The one live rollout: the newest activated candidate with percent > 0."""
        live = [rollout for rollout in self._rollouts.values() if rollout.percent > 0]
        if not live:
            return None
        return max(live, key=lambda rollout: rollout.candidate_id)


#: The process-wide controller the API endpoints and the pipeline share.
rollout = CandidateRollout()


__all__ = [
    "CandidateRollout",
    "PERCENT_NOTE_KEY",
    "RolloutSnapshot",
    "bucket",
    "candidate_question_set_version",
    "percent_note",
    "rollout",
]
