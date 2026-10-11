"""点踩 → 观测：把用户在替你标注的失败接进回路。

动机（实测，2026-10-11）：`POST /feedback` 此前只写 users 库的 `add_feedback`，**完全不碰进化队列** ——
而点踩是**带人工标注**的最高信号失败源，正是"真实供给为 0"最该修的地方。
"""

from __future__ import annotations

from types import SimpleNamespace

import rag.api as api


class _FakeUserDB:
    def __init__(self, question="年假是多少", reliability=None):
        self._question = question
        self._reliability = reliability or {"answerability_kind": "insufficient_evidence", "confidence_level": "low"}

    def get_message_question(self, message_id):
        return self._question

    def get_message_reliability(self, message_id):
        return self._reliability


class _FakeRecorder:
    def __init__(self, result=True):
        self.result = result
        self.submitted = []

    def submit(self, trace, detail=None):
        self.submitted.append((trace, detail))
        return self.result


def _request(message_id=7, value="negative", comment=None):
    return SimpleNamespace(message_id=message_id, value=value, comment=comment)


def test_a_thumbs_down_becomes_an_observation(monkeypatch):
    """负向反馈 ⇒ 一条观测：来源如实区分、带结构化证据、**不含留言文字**。"""
    recorder = _FakeRecorder()
    monkeypatch.setattr(api, "get_failure_recorder", lambda: recorder, raising=True)

    ok = api._submit_feedback_observation(_FakeUserDB(), _request(comment="答错了"))

    assert ok is True
    trace, detail = recorder.submitted[0]
    assert trace.route == "feedback"          # 用户报告的失败，不是路由判定出来的
    assert trace.question == "年假是多少"
    assert detail.answerability_kind == "insufficient_evidence"   # 结构化证据来自消息本身
    assert detail.feedback_kind == "negative_with_comment"
    # 红线：留言文字绝不入库（只记"有/无"）。
    assert "答错了" not in str(detail.model_dump())


def test_an_empty_question_is_skipped(monkeypatch):
    recorder = _FakeRecorder()
    monkeypatch.setattr(api, "get_failure_recorder", lambda: recorder, raising=True)

    assert api._submit_feedback_observation(_FakeUserDB(question=""), _request()) is False
    assert recorder.submitted == []


def test_a_recorder_failure_never_breaks_feedback(monkeypatch):
    """记录失败绝不能影响反馈本身——用户点了踩就该成功返回。"""
    class _Boom(_FakeRecorder):
        def submit(self, trace, detail=None):
            raise RuntimeError("store down")

    monkeypatch.setattr(api, "get_failure_recorder", lambda: _Boom(), raising=True)

    assert api._submit_feedback_observation(_FakeUserDB(), _request()) is False
