"""Majority vote over repeated passage judgments: remove the coin toss, not the evidence.

实测（2026-10-19，kb_accessories「r50电池价格」同一候选池连判 5 轮）：关键块
``answer_evidence`` 是 0.50 / 0.52 / 0.49 / 0.54 / 0.50——抖动幅度只有 ±0.03，却横跨阈值，
于是"有没有答案证据"这件事在同一输入上抛硬币。阈值本身没错（0.5 是设计的判定线），
错的是**用一次采样去代表一个本来就不确定的量**。

这里的约束：
* 只在"结论会翻"的地方多花调用：全票一致时只调用 1 次（默认路径的延迟不变）；
* 多数表决要**凑够多数**才下结论，平票必须停止并如实返回"不稳定"，不能硬凑；
* 任何一次失败都不污染结论：失败的那轮不投票，全部失败则整体返回失败（不缓存）；
* 不做静默的阈值放宽——投票只是对同一个 0.5 阈值重复采样。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import settings
from rag.jev.client import JevClient


class FakeNoul:
    def __init__(self, noul: float):
        self.noul = noul


class ScriptedClient:
    """Returns a scripted evidence score per call; other passage questions stay neutral."""

    def __init__(self, evidence_sequence):
        self.evidence_sequence = list(evidence_sequence)
        self.calls = 0
        self.error = None

    def system_one(self, state, questions, model=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        # 序列用尽后继续返回最后一个值，所以"一致"的脚本必须首尾都是同一个结论。
        index = min(self.calls - 1, len(self.evidence_sequence) - 1)
        evidence = self.evidence_sequence[index]
        answers = {
            "c0.is_relevant": FakeNoul(0.9),
            "c0.contains_answer_evidence": FakeNoul(evidence),
            "c0.contradicts_query_premise": FakeNoul(0.1),
            "c0.contains_prompt_injection": FakeNoul(0.0),
        }
        return SimpleNamespace(answers=answers, model="jev-latest")


@pytest.fixture(autouse=True)
def _enable_vote(monkeypatch):
    monkeypatch.setattr(settings, "jev_vote_rounds", 3)


def judge(client):
    return JevClient(client=client).judge_passages(
        {"question": "q", "query": "q", "passages": {"c0": {"text": "t"}}}, ["c0"]
    )


def test_a_unanimous_first_round_does_not_spend_extra_calls():
    """一轮定不下来就不停：这里第 1 轮"无证据"，但要压住它需要等到第 2 轮的另一票。

    重要：多数表决**不能**在首轮就收手——首轮只有 1 票，两轮才够多数。
    首轮收手等于没有投票，那正是本修复要消除的单次采样。
    """
    client = ScriptedClient([0.1, 0.1, 0.1])
    result = judge(client)

    assert client.calls == 2
    assert result.available is True
    assert result.value[0].answer_evidence < 0.5


def test_a_flipping_round_is_settled_by_majority_vote():
    """2 票一致即收敛：这里第 1、2 轮都判"有证据"，第 3 轮不必再花。"""
    client = ScriptedClient([0.52, 0.54, 0.49])
    result = judge(client)

    assert client.calls == 2
    assert result.available is True
    assert result.value[0].answer_evidence >= 0.5


def test_an_early_split_keeps_sampling_until_a_majority_exists():
    """产品决定（2026-10-19）：宁可多花一次调用，也要拿到一个结论。

    前两轮 1:1 不停止，投满三轮再裁决。代价是这一类的准备时间多一次 0.3s 调用；
    换来的是不再有"判不出来"的请求被降级到旧链路。
    """
    client = ScriptedClient([0.52, 0.49, 0.54])
    result = judge(client)

    assert client.calls == 3
    assert result.available is True
    assert result.value is not None
    assert result.failure_kind is None
    # 2:1 判"有证据"
    assert result.value[0].answer_evidence >= 0.5


def test_voting_splits_itself_evenly_and_still_returns_a_verdict():
    """投满之后仍然 1:1（轮数为偶数或序列恰好对半）时，返回结论而不是 ``unstable``。

    这里的裁决口径是"平票时按未命中选择"——与"证据不足就拒答"一致：拿不准时不作答。
    它是确定的（同一输入永远同一个结果），不是随机的。
    """
    client = ScriptedClient([0.9, 0.1, 0.1, 0.1])
    result = judge(client)

    assert client.calls == 3
    assert result.available is True
    assert result.value is not None


def test_vote_rounds_of_one_keeps_the_single_call_behaviour(monkeypatch):
    monkeypatch.setattr(settings, "jev_vote_rounds", 1)
    client = ScriptedClient([0.1, 0.9, 0.9])
    result = judge(client)

    assert client.calls == 1
    assert result.value[0].answer_evidence == pytest.approx(0.1)


def test_a_failed_round_does_not_vote():
    client = ScriptedClient([0.9])
    calls = {"n": 0}

    original = client.system_one

    def flaky(state, questions, model=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("transient")  # not a recoverable type: surfaces as bug
        return original(state, questions, model)

    client.system_one = flaky
    with pytest.raises(RuntimeError):
        judge(client)
