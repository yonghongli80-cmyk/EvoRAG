"""Pin the product decision for the redacted-price cases.

real-017/real-018 ask for R50 battery prices. The corpus keeps the price column as
``<金额>`` placeholders, so the evidence row exists but the amount is unusable. The
agreed behavior is to refuse ("证据不足就拒答"), not to lower any threshold. These
labels are asserted so a future edit cannot silently turn the known limitation into
an "expected answer" and then judge the refusal as a regression.
"""

from __future__ import annotations

import json
from pathlib import Path

CASES = Path(__file__).resolve().parents[1] / "benchmarks" / "real-cases-v1.json"


def _cases() -> list[dict]:
    payload = json.loads(CASES.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else payload.get("cases", [])


def test_redacted_price_cases_expect_refusal():
    by_id = {case["id"]: case for case in _cases()}
    for case_id in ("real-017", "real-018"):
        case = by_id[case_id]
        assert case["expected_route"] == "rag"
        assert case["expected_position"] == "refuse"
        assert case["expected_source_names"] == []


def test_redacted_price_notes_mention_the_placeholder():
    by_id = {case["id"]: case for case in _cases()}
    notes = " ".join(by_id[case_id].get("notes") or "" for case_id in ("real-017", "real-018"))
    assert "金额" in notes
    assert "<" in notes
