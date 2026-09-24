"""Task 3: structured diagnosis drafts from a versioned prompt.

Every test injects a fake ``model_call`` so nothing here performs a network
call, and the prompt/request/draft text is asserted free of credentials
(sentinel assertions). The hard post-validation rules under test:

* no admin-confirmed labels  -> ``confidence == "low"`` and root cause is a
  hypothesis ("假设");
* reproduction did not hold   -> ``confidence == "low"`` and root cause is
  marked "待复核" (recommend ``needs_more_evidence``);
* the draft's ``reproducible`` flag must agree with the pack's deterministic
  reproduction check (design section 5/9: unreproducible never enters a path).
"""

import json

import httpx
import pytest
from openai import APITimeoutError, RateLimitError

from rag.evolution.diagnoser import (
    DIAGNOSIS_PROMPT_VERSION,
    Diagnoser,
    DiagnosisUnavailable,
)
from rag.evolution.models import DiagnosisDraft, EvidencePack, ReproductionResult

SECRET_SENTINEL = "sk-diagnoser-sentinel-do-not-leak"


class FakeModel:
    """Scripted model boundary: one reply (or exception) per call."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.requests: list[list[dict]] = []

    def __call__(self, messages: list[dict]) -> str:
        self.requests.append([dict(message) for message in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return str(reply)


def _payload(**overrides: object) -> dict:
    """A contract-complete model answer for a reproducible, labelled cluster."""
    payload = {
        "failure_category": "recall_miss",
        "stage": "retrieval",
        "first_divergence": "查询改写保留了“金额”，但重检索仍未召回合同金额条款。",
        "direct_evidence": "失败时缺失的 hash-c 在重检索后依旧缺失。",
        "root_cause": "假设：召回索引未覆盖该条款，需要复核。",
        "impact_scope": "同 kb 内该聚类 3 次命中，影响金额类问题。",
        "suggested_layer": "retrieval_config",
        "reproduction_steps": "用原问题重检索，核对块哈希集合是否仍缺失 hash-c。",
        "acceptance_criteria": "原问题重检索必须命中合同金额条款块。",
        "confidence": "low",
        "reproducible": True,
        "evidence_refs": ["hash-a", "hash-c"],
    }
    payload.update(overrides)
    return payload


def _pack(**overrides: object) -> EvidencePack:
    """A deterministic-first evidence pack (admin labels + reproducible)."""
    fields = {
        "cluster_id": 7,
        "kb_id": "kb-finance",
        "question_samples": ("合同金额是多少？",),
        "failure_stage": "retrieval",
        "root_cause_feature": "recall_miss",
        "admin_labels": ({"expected_route": "rag", "expected_answer": "金额为 100 万元"},),
        "success_traces": ({"question": "同类问题", "adopted_evidence_hashes": ["hash-ok"]},),
        "recall_hashes": ("hash-a", "hash-b"),
        "failed_hashes": ("hash-c",),
        "reproduction": ReproductionResult(reproducible=True, missing_hashes=(), note="重检索块集合一致"),
    }
    fields.update(overrides)
    return EvidencePack(**fields)


def _cluster() -> dict:
    return {
        "id": 7,
        "kb_id": "kb-finance",
        "failure_stage": "retrieval",
        "root_cause_feature": "recall_miss",
        "pool": "candidate_pool",
        "status": "awaiting_diagnosis_review",
        "priority": "high",
        "member_count": 3,
    }


def _request_text(fake: FakeModel) -> str:
    return "".join(str(message.get("content", "")) for request in fake.requests for message in request)


def test_valid_json_yields_nine_field_draft_and_records_prompt_version():
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))
    diagnoser = Diagnoser(model_call=fake)

    draft = diagnoser.draft(_cluster(), _pack())

    assert isinstance(draft, DiagnosisDraft)
    assert draft.model_dump() == DiagnosisDraft.model_validate(_payload()).model_dump()
    # The nine design-section-6 content fields are all carried through.
    assert draft.failure_category == "recall_miss"
    assert draft.suggested_layer == "retrieval_config"
    assert draft.evidence_refs == ("hash-a", "hash-c")
    # Prompt version is traceable on the instance and inside the request.
    assert DIAGNOSIS_PROMPT_VERSION == "diagnosis-v1"
    assert diagnoser.prompt_version == DIAGNOSIS_PROMPT_VERSION
    assert DIAGNOSIS_PROMPT_VERSION in _request_text(fake)


def test_prompt_version_is_injectable_for_future_prompt_revisions():
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))
    diagnoser = Diagnoser(model_call=fake, prompt_version="diagnosis-v2")

    assert diagnoser.prompt_version == "diagnosis-v2"
    diagnoser.draft(_cluster(), _pack())
    assert "diagnosis-v2" in _request_text(fake)


def test_missing_admin_labels_forces_low_confidence_with_hypothesis_root_cause():
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))
    pack = _pack(admin_labels=())

    draft = Diagnoser(model_call=fake).draft(_cluster(), pack)

    assert draft.confidence == "low"
    assert "假设" in draft.root_cause


@pytest.mark.parametrize(
    "overrides",
    [
        {"confidence": "medium"},
        {"confidence": "high"},
        {"root_cause": "结论：召回索引未覆盖该条款。"},
    ],
)
def test_missing_admin_labels_rejects_non_compliant_draft(overrides):
    fake = FakeModel(json.dumps(_payload(**overrides), ensure_ascii=False))
    pack = _pack(admin_labels=())

    with pytest.raises(DiagnosisUnavailable) as excinfo:
        Diagnoser(model_call=fake).draft(_cluster(), pack)

    assert excinfo.value.reason == "invalid"


def test_non_reproducible_forces_low_confidence_and_pending_review():
    overrides = {
        "confidence": "low",
        "reproducible": False,
        "root_cause": "待复核：复现检查未通过，建议管理员执行 needs_more_evidence。",
    }
    fake = FakeModel(json.dumps(_payload(**overrides), ensure_ascii=False))
    pack = _pack(
        reproduction=ReproductionResult(reproducible=False, missing_hashes=("hash-c",), note="重检索命中不同块集合")
    )

    draft = Diagnoser(model_call=fake).draft(_cluster(), pack)

    assert draft.reproducible is False
    assert draft.confidence == "low"
    assert "待复核" in draft.root_cause
    assert "needs_more_evidence" in draft.root_cause


@pytest.mark.parametrize(
    "overrides",
    [
        {"confidence": "medium", "reproducible": False},
        {"confidence": "high", "reproducible": False},
        {
            "reproducible": False,
            "root_cause": "结论：召回索引未覆盖该条款。",
        },
        {"confidence": "low", "reproducible": True},
    ],
)
def test_non_reproducible_rejects_non_compliant_draft(overrides):
    fake = FakeModel(json.dumps(_payload(**overrides), ensure_ascii=False))
    pack = _pack(
        reproduction=ReproductionResult(reproducible=False, missing_hashes=("hash-c",), note="重检索命中不同块集合")
    )

    with pytest.raises(DiagnosisUnavailable) as excinfo:
        Diagnoser(model_call=fake).draft(_cluster(), pack)

    assert excinfo.value.reason == "invalid"


@pytest.mark.parametrize(
    "reply, reason",
    [
        ("抱歉，我无法处理这个请求。", "invalid"),
        ("[]", "invalid"),
        (json.dumps(_payload(acceptance_criteria=None)), "invalid"),
        (
            json.dumps({k: v for k, v in _payload().items() if k != "acceptance_criteria"}),
            "invalid",
        ),
        (json.dumps(_payload(failure_category="not_in_taxonomy")), "invalid"),
        (json.dumps(_payload(suggested_layer="unknown_layer")), "invalid"),
        (TimeoutError("timed out"), "timeout"),
        (APITimeoutError(request=httpx.Request("POST", "https://provider.example/v1")), "timeout"),
        (
            RateLimitError(
                "rate limited",
                response=httpx.Response(429, request=httpx.Request("POST", "https://provider.example/v1")),
                body=None,
            ),
            "rate_limited",
        ),
    ],
)
def test_model_failures_raise_diagnosis_unavailable_without_draft(reply, reason):
    fake = FakeModel(reply)

    with pytest.raises(DiagnosisUnavailable) as excinfo:
        Diagnoser(model_call=fake).draft(_cluster(), _pack())

    assert excinfo.value.reason == reason
    assert len(fake.requests) == 1  # one call per cluster, no retry storm


def test_prompt_and_draft_carry_no_credentials(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", SECRET_SENTINEL)
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))
    diagnoser = Diagnoser(model_call=fake)

    draft = diagnoser.draft(_cluster(), _pack())

    assert SECRET_SENTINEL not in _request_text(fake)
    assert SECRET_SENTINEL not in draft.model_dump_json()
    assert SECRET_SENTINEL not in str(diagnoser.last_meta)


def test_prompt_contract_declares_evidence_only_taxonomy_and_json_shape():
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))

    Diagnoser(model_call=fake).draft(_cluster(), _pack())

    prompt = _request_text(fake)
    # Only pack evidence counts; hashes/labels must not be invented.
    assert "证据包" in prompt
    assert "只依据" in prompt
    assert "禁止编造" in prompt
    # Design section 6 taxonomy is stated for the model.
    from rag.evolution.models import DiagnosisDraft as Draft

    for value in Draft.model_fields["failure_category"].annotation.__args__:
        assert value in prompt
    for value in Draft.model_fields["suggested_layer"].annotation.__args__:
        assert value in prompt
    # Output shape: one JSON object, every field present.
    assert "单个 JSON 对象" in prompt
    for field in Draft.model_fields:
        assert field in prompt
    # Version stamp travels with the prompt.
    assert DIAGNOSIS_PROMPT_VERSION in prompt


def test_default_model_call_delegates_to_generator_generate_with_low_temperature(monkeypatch):
    import rag.generator as generator

    recorded: dict[str, object] = {}

    def fake_generate(messages, temperature=0.3):
        recorded["messages"] = messages
        recorded["temperature"] = temperature
        return json.dumps(_payload(), ensure_ascii=False)

    monkeypatch.setattr(generator, "generate", fake_generate)

    draft = Diagnoser().draft(_cluster(), _pack())

    assert draft.failure_category == "recall_miss"
    assert recorded["temperature"] == 0.2
    assert isinstance(recorded["messages"], list)
    assert recorded["messages"][0]["role"] == "system"
