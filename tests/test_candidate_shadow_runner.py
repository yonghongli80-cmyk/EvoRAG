import json
from pathlib import Path

import pytest

import scripts.run_candidate_shadow as runner


class _FakeDecision:
    route = "rag"
    reason = ""


class _FakeService:
    def route(self, question, capabilities):
        return _FakeDecision()

    def select(self, question, candidates, *, query=None):
        raise AssertionError("run_shadow submits no candidates; select must not run")


class _FakeStore:
    def __init__(self):
        self.entries = []

    def record_gate_entry(self, candidate_id, **kwargs):
        self.entries.append((candidate_id, kwargs))
        return {"id": len(self.entries)}


@pytest.fixture(autouse=True)
def _fake_jev(monkeypatch):
    monkeypatch.setattr(runner, "JevDecisionService", lambda client: _FakeService())
    monkeypatch.setattr(runner, "JevClient", lambda **kwargs: object())


def _candidate():
    return {
        "id": 30,
        "artifact_type": "jev_question_set",
        "target": "passages",
        "payload": {"c0": {"instructions": "x"}},
    }


def _cases():
    return [
        {"id": "a", "question": "问题一", "kb_id": "kb"},
        {"id": "b", "question": "问题二", "kb_id": "kb"},
    ]


def test_run_shadow_records_one_gate_entry_with_real_samples():
    store = _FakeStore()
    report = runner.run_shadow(_candidate(), _cases(), store=store, actor="tester")

    assert report["result"] == "passed"
    assert report["samples"] == 2
    assert report["recorded"] is True
    assert len(store.entries) == 1
    candidate_id, entry = store.entries[0]
    assert candidate_id == 30
    assert entry["gate"] == "shadow"
    assert entry["result"] == "passed"
    assert entry["gate_metrics"]["samples"] == 2
    assert entry["gate_metrics"]["route_agreement"] == 1.0


def test_dry_run_never_writes_a_gate_entry():
    store = _FakeStore()
    report = runner.run_shadow(
        _candidate(), _cases(), store=store, actor="tester", dry_run=True
    )

    assert report["recorded"] is False
    assert store.entries == []


def test_non_question_set_candidate_is_rejected():
    with pytest.raises(ValueError, match="jev_question_set"):
        runner.run_shadow(
            {**_candidate(), "artifact_type": "code_patch"},
            _cases(),
            store=_FakeStore(),
            actor="tester",
        )


def test_load_cases_accepts_document_and_plain_list(tmp_path: Path):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps({"cases": [{"question": "q", "kb_id": "kb"}, {"bad": True}]}),
        encoding="utf-8",
    )
    cases = runner._load_cases(path, 20)
    assert cases == [{"id": "", "question": "q", "kb_id": "kb"}]
