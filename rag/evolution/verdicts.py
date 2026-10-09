"""Administrator verdicts that decide whether a recorded failure is a defect at all.

Not every recorded failure is a defect. Four of the twelve questions an audit could not classify
are requests the system *should* refuse — an administrator asking for database credentials, a
bulk dump of every document, a meta question — and it refused them correctly. Left unfiltered
they would feed the loop's later stages: a knowledge-gap ticket would be drafted asking somebody
to go and add the very content that must not be handed over.

No shape-based rule can separate them. Measured: those four observations look exactly like the
confirmed content gaps — ``route=fixed``, ``answerability=insufficient_evidence``, zero chunks —
so routing, answerability, scores and hashes carry no signal at all. The only thing that does is
a human judgement, which is why verdicts live in the same admin-confirmed labels file the
diagnosis already trusts, as ``"verdict": "not_a_defect"`` next to the note that explains it.

This module reads those verdicts and answers one question: should this question be worked on at
all? It is pure and never writes; a missing or unreadable labels file simply yields no verdicts,
which is the same behaviour the loop had before the markers existed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

#: The verdict that marks a recorded failure as correct behaviour rather than a defect.
NOT_A_DEFECT = "not_a_defect"


def normalize(question: str) -> str:
    """Whitespace-insensitive question key, matching how labels are matched elsewhere."""
    return " ".join(str(question or "").split())


def load_verdicts(labels_path: str | Path | None) -> dict[str, str]:
    """``{normalized question: verdict}`` for every label that carries a verdict.

    Only ``not_a_defect`` is recognised today; an unknown verdict is ignored rather than
    guessed at, so a typo in the file cannot silently silence a real defect.
    """
    if not labels_path:
        return {}
    try:
        raw = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, list):
        return {}
    verdicts: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        verdict = str(entry.get("verdict") or "")
        if verdict != NOT_A_DEFECT:
            continue
        key = normalize(str(entry.get("question") or ""))
        if key:
            verdicts[key] = verdict
    return verdicts


def not_a_defect_questions(labels_path: str | Path | None) -> frozenset[str]:
    """The normalized questions an administrator marked as correct behaviour."""
    return frozenset(load_verdicts(labels_path))


def is_not_a_defect(question: str, verdicts: Iterable[str]) -> bool:
    """Whether ``question`` is among the marked questions (both sides normalized)."""
    return normalize(question) in set(verdicts)


def filter_defects(observations: Iterable[Mapping], verdicts: Iterable[str]) -> list[Mapping]:
    """The observations that are still candidates for work.

    Order is preserved so callers can keep their own ranking, and an observation without a
    question is kept: judging it would require a verdict we do not have.
    """
    marked = set(verdicts)
    return [
        observation
        for observation in observations
        if not is_not_a_defect(str(observation.get("question") or ""), marked)
    ]


__all__ = [
    "NOT_A_DEFECT",
    "filter_defects",
    "is_not_a_defect",
    "load_verdicts",
    "normalize",
    "not_a_defect_questions",
]
