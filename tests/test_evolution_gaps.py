"""知识缺口工单的草稿器：全部确定性，不花模型调用。

动机（实测）：78% 的失败是"库里根本没有"（73 个不同问题里 57 个），而回路的执行器当时只有
"改提示词/改代码"两种——undetermined 聚类因此停在观察队列里无事可做。工单**不是候选**：它不服务
流量、不进候选池、不改任何状态机，只是把一堆同样的拒绝变成一份人可以照着补内容的请求。
"""

from __future__ import annotations

from rag.evolution.gaps import (
    KnowledgeGapTicket,
    aggregate_missing_terms,
    build_ticket,
    missing_terms_for,
    question_samples,
)


def _observation(question, created_at):
    return {"question": question, "created_at": created_at, "kb_id": "kb1"}


def test_question_samples_dedup_normalize_and_cap():
    observations = [
        _observation("R50 电池卖多少钱", 1.0),
        _observation("R50  电池卖多少钱", 2.0),  # 空白差异 ⇒ 同一个问法
        _observation("人员盘点覆盖哪些人", 3.0),
        _observation("回收岗是做什么的", 4.0),
    ]

    samples = question_samples(observations, limit=2)

    assert samples == ("R50 电池卖多少钱", "人员盘点覆盖哪些人")


def test_missing_terms_names_only_terms_the_recall_lacks():
    texts = ["佳能 EOS R50 电池型号 LP-E17", "无关的一段话"]

    absent = missing_terms_for("R50 电池卖多少钱", texts)

    assert "r50" not in absent  # 召回到的术语不算缺失
    assert "电池" in absent or "卖多" in absent  # 没被召回的说法要报出来
    assert all(term not in " ".join(texts) for term in absent)


def test_aggregate_missing_terms_ranks_by_frequency_then_first_appearance():
    ranked = aggregate_missing_terms(
        [("电池", "价格"), ("电池", "型号"), ("电池",)],
        limit=3,
    )

    assert ranked == ("电池", "价格", "型号")


def test_build_ticket_carries_the_numbers_and_the_absent_terms():
    observations = [_observation("R50 电池卖多少钱", 10.0), _observation("R50电池多少钱", 30.0)]
    cluster = {"id": 7, "kb_id": "kb_accessories"}

    ticket = build_ticket(
        cluster, observations, missing_terms=("电池", "价格"), probed_questions=2, now=100.0
    )

    assert isinstance(ticket, KnowledgeGapTicket)
    assert ticket.cluster_id == 7 and ticket.kb_id == "kb_accessories"
    assert ticket.occurrences == 2
    assert (ticket.first_seen, ticket.last_seen) == (10.0, 30.0)
    assert ticket.probed_questions == 2
    assert "电池" in ticket.rationale and "价格" in ticket.rationale
    assert "覆盖度信号" in ticket.rationale  # 明说这不是判决
    assert ticket.to_payload()["missing_terms"] == ["电池", "价格"]


def test_a_ticket_without_probe_says_so_instead_of_inventing_terms():
    ticket = build_ticket({"id": 1, "kb_id": "kb1"}, [_observation("q", 1.0)], now=5.0)

    assert ticket.missing_terms == ()
    assert "未探测" in ticket.rationale
