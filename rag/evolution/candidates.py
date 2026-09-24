"""Candidate contracts for the assisted self-evolution phase (phase 4).

Only one artifact type exists in this phase: ``jev_question_set`` — a text
variant of the Jev judgement layer's question set. It is pure data, it can be
shadow-compared against the live set question by question, and it rolls back
instantly through the version recorded in ``rollback_ref``.

The payload is a **text-only override**. Question ids, question count, primitive
type (``Choice``/``Noul``) and the criteria key set are structural: the
deterministic mappers (``_map_preflight``/``_map_passages``) and the stored
schema depend on them, so :func:`validate_payload` rejects any payload that
touches them. Two text bounds keep a candidate a surgical edit rather than a
rewrite: a per-field length limit and a per-field change magnitude of 40%.

This module is pure: it reads the current question set passed in by the caller
(``rag.jev.questions`` builders in production, synthetic sets in tests) and never
mutates it. It does not touch the database, the model client, or the API.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

#: The only artifact type this phase may produce (design §6 suggestion layers).
ARTIFACT_TYPE = "jev_question_set"

#: The only two question-set targets a candidate may address.
TARGETS: tuple[str, ...] = ("preflight", "passages")

# A payload entry may carry these two text fields and nothing else.
_OVERRIDE_KEYS: tuple[str, ...] = ("instructions", "criteria")
_MAX_INSTRUCTIONS_CHARS = 500
_MAX_CRITERIA_CHARS = 200

# Deterministic change bound: at most 40% of a field's characters may differ
# from the base text it replaces (Levenshtein distance / longest side).
_MAX_CHANGE_RATIO = 0.40

# A rollback reference must name the live question-set version and hash the
# payload snapshot a rollback restores; the digest is the house sha256 hex form.
_ROLLBACK_REF_VERSION_KEY = "question_set_version"
_ROLLBACK_REF_HASH_KEY = "payload_sha256"
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


class PayloadBoundaryError(ValueError):
    """A candidate payload stepped outside the structural boundary.

    ``reason`` carries question ids, lengths, counts and key names only — never
    question text — so a rejected payload cannot leak knowledge-base content
    into a log, a gate note, or an API error.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _as_text(value: object) -> str:
    """Canonical text for a question field, so base and override compare fairly."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _question_parts(question: object) -> tuple[object, dict[str, object]]:
    """Read ``(instructions, criteria)`` off a question object or question dict."""
    if isinstance(question, Mapping):
        instructions = question.get("instructions")
        raw_criteria = question.get("criteria")
    else:
        instructions = getattr(question, "instructions", None)
        raw_criteria = getattr(question, "criteria", None)
    criteria = dict(raw_criteria) if isinstance(raw_criteria, Mapping) else {}
    return instructions, criteria


def _levenshtein(left: str, right: str) -> int:
    """Character-level edit distance; deterministic and dependency-free."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def _change_ratio(base_text: str, new_text: str) -> float:
    """Share of characters that changed, against the longer of the two texts."""
    longest = max(len(base_text), len(new_text))
    if longest == 0:
        return 0.0
    return _levenshtein(base_text, new_text) / longest


def _validate_change(
    question_id: str,
    base_instructions: object,
    base_criteria: Mapping[str, object],
    entry: Mapping[str, object],
) -> None:
    """Enforce the ≤40% change magnitude per overridden field.

    Each overridden field is compared with the base text it replaces, so a
    heavy rewrite of one instruction or one criteria line is refused even when
    the rest of that question (and the whole set) is untouched.
    """
    new_instructions = entry.get("instructions", base_instructions)
    ratio = _change_ratio(_as_text(base_instructions), _as_text(new_instructions))
    if ratio > _MAX_CHANGE_RATIO:
        raise PayloadBoundaryError(
            f"{question_id}: instructions changed {ratio:.0%}, limit {_MAX_CHANGE_RATIO:.0%}"
        )
    if "criteria" not in entry:
        return
    for label, value in entry["criteria"].items():
        ratio = _change_ratio(_as_text(base_criteria.get(label)), _as_text(value))
        if ratio > _MAX_CHANGE_RATIO:
            raise PayloadBoundaryError(
                f"{question_id}: criteria[{label!r}] changed {ratio:.0%}, "
                f"limit {_MAX_CHANGE_RATIO:.0%}"
            )


def _validate_entry(question_id: str, entry: Mapping[str, object], base_question: object) -> None:
    """Structural and text checks for one payload entry against its base question."""
    unsupported = sorted(set(entry) - set(_OVERRIDE_KEYS))
    if unsupported:
        raise PayloadBoundaryError(
            f"{question_id}: only {list(_OVERRIDE_KEYS)} may be overridden, got {unsupported}"
        )
    base_instructions, base_criteria = _question_parts(base_question)
    if "instructions" in entry:
        text = entry["instructions"]
        if not isinstance(text, str):
            raise PayloadBoundaryError(f"{question_id}: instructions must be a string")
        if not text.strip():
            raise PayloadBoundaryError(f"{question_id}: instructions is empty")
        if len(text) > _MAX_INSTRUCTIONS_CHARS:
            raise PayloadBoundaryError(
                f"{question_id}: instructions is {len(text)} characters, "
                f"limit {_MAX_INSTRUCTIONS_CHARS}"
            )
    if "criteria" in entry:
        criteria = entry["criteria"]
        if not isinstance(criteria, Mapping):
            raise PayloadBoundaryError(f"{question_id}: criteria must be a mapping")
        if set(criteria) != set(base_criteria):
            raise PayloadBoundaryError(
                f"{question_id}: criteria keys {sorted(criteria)} must match the base keys "
                f"{sorted(base_criteria)}"
            )
        for label, value in criteria.items():
            if not isinstance(value, str):
                raise PayloadBoundaryError(f"{question_id}: criteria[{label!r}] must be a string")
            if not value.strip():
                raise PayloadBoundaryError(f"{question_id}: criteria[{label!r}] is empty")
            if len(value) > _MAX_CRITERIA_CHARS:
                raise PayloadBoundaryError(
                    f"{question_id}: criteria[{label!r}] is {len(value)} characters, "
                    f"limit {_MAX_CRITERIA_CHARS}"
                )
    _validate_change(question_id, base_instructions, base_criteria, entry)


def validate_payload(target: str, payload: dict, base: Mapping[str, object]) -> None:
    """Assert a candidate payload stays inside the question-set boundary.

    ``base`` is the current question set for ``target`` (question id → question
    object or question dict); ``payload`` maps the same ids to text overrides.
    The check is deterministic and never needs the model:

    1. ``payload`` is non-empty and changes at least one question.
    2. Its id set equals the base id set: no question may be added, dropped, or
       renamed, so the question count is unchanged.
    3. Every entry carries only ``instructions``/``criteria`` — the payload has
       no place to state a primitive type, so ``Choice``/``Noul`` is preserved
       by construction.
    4. A ``criteria`` override keeps the base criteria key set exactly.
    5. ``instructions`` is ≤500 characters and each criteria value ≤200, both
       non-empty strings.
    6. Each overridden field differs from the base text it replaces by at most
       40% of its characters (Levenshtein distance / longest side).
    7. ``target`` is one of :data:`TARGETS`; anything else is a programming
       error and raises ``ValueError`` instead of a boundary error.

    Raises :class:`PayloadBoundaryError` for a boundary violation and
    ``ValueError`` for an unknown target. Returns ``None`` on success.
    """
    if target not in TARGETS:
        raise ValueError(f"unknown candidate target: {target!r}")
    if not isinstance(payload, Mapping) or not payload:
        raise PayloadBoundaryError("payload is empty; a candidate must override at least one question")
    if not isinstance(base, Mapping) or not base:
        raise PayloadBoundaryError("base question set is empty; nothing to compare against")

    base_ids = set(base)
    unknown_ids = sorted(set(payload) - base_ids)
    if unknown_ids:
        raise PayloadBoundaryError(f"unknown question id(s) {unknown_ids}; new ids are not allowed")
    missing_ids = sorted(base_ids - set(payload))
    if missing_ids:
        raise PayloadBoundaryError(f"missing question id(s) {missing_ids}; question count must not change")
    if all(not entry for entry in payload.values()):
        raise PayloadBoundaryError("payload changes no question text")

    for question_id in sorted(payload):
        entry = payload[question_id]
        if not isinstance(entry, Mapping):
            raise PayloadBoundaryError(
                f"{question_id}: override must be a mapping of {list(_OVERRIDE_KEYS)}"
            )
        _validate_entry(question_id, entry, base[question_id])


class JevQuestionSetCandidate(BaseModel):
    """One question-set candidate: a bounded text variant of the live set.

    ``extra="forbid"`` makes an unknown field (a chunk text, an answer, a
    credential) a construction error, exactly like ``DiagnosticDraft``.
    ``rollback_ref`` must name the live question-set version and hash the
    payload snapshot a rollback restores, because rollback may only depend on
    the stored record (design §13: instant, self-contained rollback).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_type: Literal["jev_question_set"]
    target: Literal["preflight", "passages"]
    payload: dict[str, dict]
    rationale: str
    expected_effect: str
    rollback_ref: dict
    confidence: Literal["low", "medium", "high"]

    @field_validator("payload")
    @classmethod
    def _payload_shape(cls, value: dict[str, dict]) -> dict[str, dict]:
        """Structural shape at construction time; the boundary itself needs ``base``."""
        if not value:
            raise ValueError("payload is empty; a candidate must override at least one question")
        for question_id, entry in value.items():
            if not isinstance(entry, dict):
                raise ValueError(f"{question_id}: override must be a mapping")
            unsupported = sorted(set(entry) - set(_OVERRIDE_KEYS))
            if unsupported:
                raise ValueError(
                    f"{question_id}: only {list(_OVERRIDE_KEYS)} may be overridden, got {unsupported}"
                )
            if "instructions" in entry and not isinstance(entry["instructions"], str):
                raise ValueError(f"{question_id}: instructions must be a string")
            criteria = entry.get("criteria")
            if "criteria" in entry:
                if not isinstance(criteria, dict):
                    raise ValueError(f"{question_id}: criteria must be a mapping")
                for label, text in criteria.items():
                    if not isinstance(text, str):
                        raise ValueError(f"{question_id}: criteria[{label!r}] must be a string")
        return value

    @field_validator("rollback_ref")
    @classmethod
    def _rollback_ref(cls, value: dict) -> dict:
        version = value.get(_ROLLBACK_REF_VERSION_KEY)
        digest = value.get(_ROLLBACK_REF_HASH_KEY)
        if not isinstance(version, str) or not version:
            raise ValueError(
                f"rollback_ref must carry the current {_ROLLBACK_REF_VERSION_KEY}"
            )
        if not isinstance(digest, str) or not _SHA256_PATTERN.match(digest):
            raise ValueError(
                f"rollback_ref must carry the {_ROLLBACK_REF_HASH_KEY} of the current payload"
            )
        return value


__all__ = [
    "ARTIFACT_TYPE",
    "TARGETS",
    "JevQuestionSetCandidate",
    "PayloadBoundaryError",
    "validate_payload",
]
