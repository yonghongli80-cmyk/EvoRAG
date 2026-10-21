"""A redacted amount must not be treated as an answer to a price question."""

from __future__ import annotations

from rag.jev.policy import PassageSelection
from rag.models import Chunk
from rag.pipeline import _drop_redacted_price_evidence


def _chunk(text: str, *, evidence: bool = True) -> Chunk:
    return Chunk(
        text=text,
        doc_name="price.xlsx",
        chunk_index=0,
        metadata={"jev_answer_evidence": "true" if evidence else "false"},
    )


def _selection(*chunks: Chunk) -> PassageSelection:
    return PassageSelection(
        accepted=tuple(chunks),
        conflicts=(),
        excluded_ids=(),
        has_answer_evidence=True,
    )


def test_redacted_price_is_not_answer_evidence():
    selection = _selection(_chunk("R50 原厂电池，价格：<金额>"))
    result = _drop_redacted_price_evidence("r50原厂电池价格", selection)

    assert result.has_answer_evidence is False
    # The chunk stays in accepted: the refusal should still cite the document it looked at.
    assert result.accepted == selection.accepted


def test_other_redaction_forms_are_recognized():
    for text in ("价格：<金额>", "价格：[REDACTED]", "价格：***"):
        result = _drop_redacted_price_evidence("这个多少钱", _selection(_chunk(text)))
        assert result.has_answer_evidence is False, text


def test_non_price_question_keeps_the_evidence():
    selection = _selection(_chunk("R50 的保修期是 12 个月，价格：<金额>"))
    result = _drop_redacted_price_evidence("R50 保修多久", selection)
    assert result.has_answer_evidence is True


def test_a_real_amount_keeps_the_evidence():
    selection = _selection(_chunk("R50 电池价格：1280 元"))
    result = _drop_redacted_price_evidence("r50电池价格", selection)
    assert result.has_answer_evidence is True


def test_one_usable_evidence_chunk_is_enough():
    selection = _selection(
        _chunk("R50 价格：<金额>"),
        _chunk("R50 价格：1280 元"),
    )
    result = _drop_redacted_price_evidence("r50电池价格", selection)
    assert result.has_answer_evidence is True


def test_no_evidence_stays_no_evidence():
    selection = PassageSelection(
        accepted=(_chunk("无关内容", evidence=False),),
        conflicts=(),
        excluded_ids=(),
        has_answer_evidence=False,
    )
    result = _drop_redacted_price_evidence("r50电池价格", selection)
    assert result.has_answer_evidence is False
