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
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, RateLimitError

from config import settings
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
    # v4 起：证据包带上判定层统计量 passage_judging，并给出对应的分类口径。
    # v5 起：异常失败（failure_route=error + exception_class）必须按缺陷判类，
    #        不得顺着管理员标签的 expected_route 解释成路由问题。
    # （试过的 v6「散文字段禁贴哈希 + 限长」在 ranking_error 族实测无改善，已撤回并留档。）
    # v7 起：label_evidence —— 管理员答案文本在召回块中的**精确包含检查**，命中即可判
    #        ranking_error（能拿到却没被采用），这是"内容存在"唯一不靠猜的确定性判据。
    assert DIAGNOSIS_PROMPT_VERSION == "diagnosis-v7"
    assert diagnoser.prompt_version == DIAGNOSIS_PROMPT_VERSION
    assert DIAGNOSIS_PROMPT_VERSION in _request_text(fake)


def test_prompt_version_is_injectable_for_future_prompt_revisions():
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))
    diagnoser = Diagnoser(model_call=fake, prompt_version="diagnosis-v2")

    assert diagnoser.prompt_version == "diagnosis-v2"
    diagnoser.draft(_cluster(), _pack())
    assert "diagnosis-v2" in _request_text(fake)


def test_prompt_renders_question_term_coverage_as_evidence():
    """覆盖度信号进提示词：逐词给出命中块数与最早排名。

    这是"埋没型缺陷"的确定性依据——关键词在召回集合里有覆盖（哪怕排名靠后）
    说明内容存在却没被采用。**只作为证据渲染，不附带新的判定口径**：第一版附了
    "有覆盖⇒ranking_error / 无覆盖⇒recall_miss"的口径，成对开发集跑显示它把内容缺口
    的行推成 recall_miss，而 ranking_error 那几行本身不稳定，故不采用口径推送。
    """
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))
    pack = _pack(
        question_term_coverage=(
            {"term": "副厂", "chunks": 3, "best_rank": 11},
            {"term": "报价", "chunks": 0, "best_rank": 0},
        )
    )

    Diagnoser(model_call=fake).draft(_cluster(), pack)

    prompt = _request_text(fake)
    assert "question_term_coverage" in prompt
    assert "term=副厂 chunks=3 best_rank=11" in prompt
    assert "term=报价 chunks=0 best_rank=0" in prompt


def test_prompt_renders_an_exception_failure_and_requires_a_defect_category():
    """异常失败必须渲染出来并按缺陷判类。

    2026-09-30：6 条 `ValueError: versioned evidence required` 观测被诊断成
    `intent_route_error`——因为异常类只进了观测、没进证据包，模型只能顺着管理员标签里的
    `expected_route=rag` 把"请求异常"解释成"路由没走 RAG"。异常必须显式出现，且类别被钉在
    代码/提示词缺陷上。
    """
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))
    pack = _pack(failure_route="error", exception_class="ValueError")

    Diagnoser(model_call=fake).draft(_cluster(), pack)

    prompt = _request_text(fake)
    assert "failure_route: error" in prompt
    assert "exception_class: ValueError" in prompt
    assert "未捕获异常" in prompt
    assert "不得**判成 intent_route_error" in prompt or "intent_route_error" in prompt


def test_prompt_has_no_exception_rule_for_ordinary_failures():
    """非异常失败不得被这条口径影响：规则只在 exception_class 非空时出现。"""
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))

    Diagnoser(model_call=fake).draft(_cluster(), _pack())

    prompt = _request_text(fake)
    assert "exception_class: （无：不是异常失败）" in prompt
    assert "未捕获异常" not in prompt


def test_a_pack_without_coverage_renders_an_explicit_empty_section():
    """没有覆盖度证据时显式写"无"，不编造 0 命中也不省略该段。"""
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))

    Diagnoser(model_call=fake).draft(_cluster(), _pack())

    prompt = _request_text(fake)
    assert "question_term_coverage" in prompt
    assert "question_term_coverage: （无）" in prompt


def test_prompt_renders_what_the_passage_judge_saw():
    """判定层统计量进提示词：候选数 + 判为相关/答案证据/冲突/注入的块数。

    这是"召回了却没用上"与"根本没召回"的分界证据。**只渲染证据，不附加强制口径**：
    2026-09-29 成对实测显示"relevant>0 且 evidence==0 ⇒ 取 prompt/query_rewrite"这条口径
    会把内容缺口推成判定层缺陷，故与覆盖度信号同样只留证据
    （real-case-handoff/evolab/diagnosis-rule-ab.json）。
    """
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))
    pack = _pack(
        passage_judging=(
            ("passage_judged", 12),
            ("passage_relevant", 5),
            ("passage_evidence", 0),
            ("passage_contradicted", 1),
            ("passage_injected", 0),
        )
    )

    Diagnoser(model_call=fake).draft(_cluster(), pack)

    prompt = _request_text(fake)
    assert "passage_judging" in prompt
    assert "passage_judged=12" in prompt
    assert "passage_evidence=0" in prompt
    # 只作为证据：不得出现"必须取 prompt/query_rewrite"这类强制口径。
    assert "必须取 prompt 或 query_rewrite" not in prompt


def test_prompt_says_when_the_judging_counts_were_not_recorded():
    """统计量缺失时显式写"未记录"，不能当作 0 参与判断。"""
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))

    Diagnoser(model_call=fake).draft(_cluster(), _pack())

    prompt = _request_text(fake)
    assert "passage_judging" in prompt
    assert "未记录" in prompt


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


def test_default_model_call_uses_an_isolated_client_with_the_configured_timeout(monkeypatch):
    """Diagnosis must not share the online generator's client or circuit breaker.

    A background timeout tripping the online breaker would take the live answer
    path down with it, so the default call builds its own client with the
    configured diagnosis timeout and the generator's provider settings.
    """
    import openai

    recorded: dict[str, object] = {}
    real_client = openai.OpenAI

    class FakeCompletions:
        def create(self, **kwargs):
            recorded.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_payload(), ensure_ascii=False)))]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            recorded["client"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    monkeypatch.setattr(settings, "deepseek_model", "deepseek/diagnosis-probe")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://provider.example/v1")
    monkeypatch.setattr(settings, "deepseek_api_key", "probe-key")
    try:
        draft = Diagnoser(timeout_seconds=90.0, max_tokens=8000).draft(_cluster(), _pack())
    finally:
        monkeypatch.setattr(openai, "OpenAI", real_client)

    assert draft.failure_category == "recall_miss"
    assert recorded["client"]["timeout"] == 90.0
    assert recorded["model"] == "deepseek/diagnosis-probe"
    assert recorded["temperature"] == 0.2
    assert recorded["max_tokens"] == 8000
    assert recorded["extra_body"] == {"thinking": {"type": "disabled"}}


def test_prompt_renders_label_evidence_and_requires_ranking_error():
    """命中 label_evidence 时必须判取用侧，而不是内容缺口或召回缺口。

    依据是确定性事实：管理员写下的答案型号串**确实出现在召回块里**（精确包含），
    因此"内容存在却没被采用"不再需要模型去猜。
    """
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))
    pack = _pack(
        label_evidence=(
            {"question": "找一下副厂配件明细", "token": "LH716-01", "chunks": 2,
             "first_rank": 3, "hash": "a" * 64},
        )
    )

    Diagnoser(model_call=fake).draft(_cluster(), pack)

    prompt = _request_text(fake)
    assert "label_evidence" in prompt
    assert "token=LH716-01 chunks=2 first_rank=3" in prompt
    assert "ranking_error" in prompt
    assert "取用/排序" in prompt


def test_label_evidence_rule_is_absent_without_a_hit():
    """没有命中时不得出现该口径：无标签的内容缺口行不能受影响。"""
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))

    Diagnoser(model_call=fake).draft(_cluster(), _pack())

    prompt = _request_text(fake)
    assert "label_evidence: （无" in prompt
    assert "取用/排序" not in prompt


def test_the_exception_rule_wins_over_label_evidence():
    """异常失败优先：即使标签答案命中了召回，也不得把这族判成取用问题。"""
    fake = FakeModel(json.dumps(_payload(), ensure_ascii=False))
    pack = _pack(
        failure_route="error",
        exception_class="ValueError",
        label_evidence=(
            {"question": "第三方配件明细查一下", "token": "SZ-B40", "chunks": 1,
             "first_rank": 0, "hash": "b" * 64},
        ),
    )

    Diagnoser(model_call=fake).draft(_cluster(), pack)

    prompt = _request_text(fake)
    assert "未捕获异常" in prompt
    assert "取用/排序" not in prompt
