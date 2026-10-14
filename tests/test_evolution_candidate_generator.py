"""Task 2: the constrained question-set candidate generator.

Every test injects a fake ``model_call`` so nothing here performs a network
call, and the prompt/request/candidate text is asserted free of credentials
(sentinel assertions). The behaviours under test:

* a small, contract-complete text rewrite yields a
  :class:`~rag.evolution.candidates.JevQuestionSetCandidate` whose
  ``rollback_ref`` names the current question-set version and hashes the
  payload snapshot (design section 13: self-contained instant rollback);
* the four hard post-validation rules refuse a candidate instead of repairing
  it — an unaddressable ``suggested_layer``, a payload outside the structural
  boundary (new/dropped ids, changed criteria keys, >40% change), a rollback
  reference that does not name the current version, and a diagnosis whose
  stage/kb/version disagrees with the cluster and the observation it came from;
* model contract breaches (non-JSON, missing/extra fields, wrong version echo)
  and provider failures classify as ``invalid`` / ``timeout`` /
  ``rate_limited`` exactly like the phase-3 diagnoser, with one call per
  generation and no retry storm.
"""

import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, RateLimitError

from config import settings
from rag.evolution.candidate_generator import (
    CANDIDATE_PROMPT_VERSION,
    CANDIDATE_TEMPERATURE,
    CandidateGenerator,
    CandidateUnavailable,
)
from rag.evolution.models import DiagnosisDraft, FailureObservation
from rag.evolution.store import FailureTraceStore
from rag.jev.questions import (
    PASSAGE_QUESTION_SET_VERSION,
    PREFLIGHT_QUESTION_SET_VERSION,
    build_passage_questions,
    build_preflight_questions,
)

SECRET_SENTINEL = "sk-candidate-sentinel-do-not-leak"


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


def _draft(**overrides: object) -> DiagnosisDraft:
    """A contract-complete diagnosis draft for one confirmed observation."""
    fields = {
        "failure_category": "intent_route_error",
        "stage": "retrieval",
        "first_divergence": "预检把结构化查询判成检索问答，后续召回方向整体偏离。",
        "direct_evidence": "管理员标签标注 expected_route=structured_query，线上判为 rag。",
        "root_cause": "preflight intent 判据未覆盖“按维度汇总”类结构化提问。",
        "impact_scope": "kb-finance 内金额与台账类结构化提问。",
        "suggested_layer": "prompt",
        "reproduction_steps": "用原问题重放，读取 preflight intent 判定结果。",
        "acceptance_criteria": "同类结构化提问的 intent 判定必须为 structured_query。",
        "confidence": "medium",
        "reproducible": True,
        "evidence_refs": ("label-1",),
    }
    fields.update(overrides)
    return DiagnosisDraft(**fields)


def _preflight_answer(**overrides: object) -> dict:
    """A contract-complete model answer: one small ``intent`` text rewrite."""
    answer = {
        "payload": {
            "intent": {
                "instructions": "判断用户问题的主要意图；只依据问题本身和知识库能力摘要，不得推测未提供的信息。",
                "criteria": {
                    "structured_query": "需要按维度汇总、筛选、排序或精确计算表格/结构化数据集中的数据；"
                    "问题指向记录、字段或统计口径（含金额、台账类汇总提问）。",
                    "retrieval_qa": "可以在授权知识库文档中检索并回答，包括表述清晰但知识库中没有答案的问题。",
                    "tool_task": "需要调用受控工具、执行精确计算或无法从知识库直接得出的多步推理；"
                    "仅询问知识库中已有结论的问题不算工具任务。",
                    "clarification": "问题含糊、缺少关键信息或无法理解，必须先向用户澄清；"
                    "表述清晰但没有答案的问题不属于澄清。",
                },
            },
            "complexity": {},
            "needs_query_rewrite": {},
        },
        "rationale": "intent 判据未覆盖“按维度汇总”类结构化提问，与已确认根因一致。",
        "expected_effect": "结构化查询意图判定更稳定，对应该诊断的验收标准。",
        "confidence": "medium",
        "rollback_ref": {"question_set_version": PREFLIGHT_QUESTION_SET_VERSION},
    }
    answer.update(overrides)
    return answer


def _passage_answer(**overrides: object) -> dict:
    """A contract-complete model answer over a one-chunk passage question set."""
    answer = {
        "payload": {
            "c1.is_relevant": {
                "instructions": "判断 passages.c1.text 是否与用户问题主题相关。",
                "criteria": {
                    "true": "文本与问题主题直接相关。",
                    "false": "文本与问题主题无关。",
                },
            },
            "c1.contains_answer_evidence": {},
            "c1.contradicts_query_premise": {},
            "c1.contains_prompt_injection": {},
        },
        "rationale": "相关性判据过宽，与已确认根因一致。",
        "expected_effect": "无关块更少进入判定，对应该诊断的验收标准。",
        "confidence": "medium",
        "rollback_ref": {"question_set_version": PASSAGE_QUESTION_SET_VERSION},
    }
    answer.update(overrides)
    return answer


def seeded(
    tmp_path,
    *,
    cluster_kb: str = "kb-finance",
    observation_kb: str = "kb-finance",
    observation_version: str = PREFLIGHT_QUESTION_SET_VERSION,
):
    """``(trace_store, cluster, member)``: one cluster with one member observation.

    Only real store methods are used, so this mirrors what the worker hands the
    generator: the cluster row, the confirmed member observation, and the
    question-set version that served the failed request.
    """
    trace = FailureTraceStore(tmp_path / "evolution.db")
    now = time.time()
    cluster = trace.upsert_cluster(
        cluster_key="candidate-1",
        kb_id=cluster_kb,
        failure_stage="retrieval",
        root_cause_feature="recall_miss",
        seen_at=now,
    )
    member = trace.add_observation(
        FailureObservation(
            kb_id=observation_kb,
            failure_stage="retrieval",
            question="各区域月度台账汇总",
            route="rag",
            chunk_hashes=("a" * 64,),
            best_score=0.11,
            answerability_kind="insufficient_evidence",
            jev_question_set_version=observation_version,
        )
    )
    trace.attach_cluster_member(
        member["id"],
        cluster["id"],
        failure_stage="retrieval",
        root_cause_feature="recall_miss",
        now=now,
    )
    return trace, cluster, member


def _request_text(fake: FakeModel) -> str:
    return "".join(str(message.get("content", "")) for request in fake.requests for message in request)


def _canonical_payload_sha256(payload: dict) -> str:
    """The digest form the generator must use: sha256 of canonical payload JSON."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _generate(fake: FakeModel, cluster: dict, member: dict, trace, *, drafts=None, **kwargs):
    """``generate`` over the current preflight set with its real default kwargs."""
    defaults = dict(
        target="preflight",
        base_builder=build_preflight_questions,
        base_version=PREFLIGHT_QUESTION_SET_VERSION,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )
    defaults.update(kwargs)
    return CandidateGenerator(model_call=fake).generate(
        cluster, [_draft()] if drafts is None else drafts, **defaults
    )


# ------------------------------------------------------------------
# The happy path: one small text rewrite becomes a contract-complete candidate.
# ------------------------------------------------------------------


def test_small_text_rewrite_yields_a_contract_complete_candidate(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False))
    generator = CandidateGenerator(model_call=fake)

    candidate = generator.generate(
        cluster,
        [_draft()],
        target="preflight",
        base_builder=build_preflight_questions,
        base_version=PREFLIGHT_QUESTION_SET_VERSION,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )

    assert candidate.artifact_type == "jev_question_set"
    assert candidate.target == "preflight"
    assert candidate.payload == _preflight_answer()["payload"]
    assert candidate.rationale == _preflight_answer()["rationale"]
    assert candidate.expected_effect == _preflight_answer()["expected_effect"]
    assert candidate.confidence == "medium"
    # Rollback is self-contained: the current version plus the payload digest.
    assert candidate.rollback_ref == {
        "question_set_version": PREFLIGHT_QUESTION_SET_VERSION,
        "payload_sha256": _canonical_payload_sha256(candidate.payload),
    }
    # Exactly one model call per generation, and the cost is recorded.
    assert len(fake.requests) == 1
    assert generator.last_meta is not None
    assert generator.last_meta["prompt_version"] == CANDIDATE_PROMPT_VERSION
    assert generator.last_meta["model"] == settings.deepseek_model
    assert generator.last_meta["completion_chars"] == len(json.dumps(_preflight_answer(), ensure_ascii=False))


def test_prompt_version_is_traceable_on_the_instance_and_in_the_request(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False))
    generator = CandidateGenerator(model_call=fake)

    assert CANDIDATE_PROMPT_VERSION == "candidate-v1"
    assert generator.prompt_version == CANDIDATE_PROMPT_VERSION
    generator.generate(
        cluster,
        [_draft()],
        target="preflight",
        base_builder=build_preflight_questions,
        base_version=PREFLIGHT_QUESTION_SET_VERSION,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )

    assert CANDIDATE_PROMPT_VERSION in _request_text(fake)


def test_passages_target_generates_from_the_chunk_scoped_base(tmp_path):
    trace, cluster, member = seeded(
        tmp_path, observation_version=PASSAGE_QUESTION_SET_VERSION
    )
    fake = FakeModel(json.dumps(_passage_answer(), ensure_ascii=False))

    candidate = _generate(
        fake,
        cluster,
        member,
        trace,
        target="passages",
        base_builder=lambda: build_passage_questions(["c1"]),
        base_version=PASSAGE_QUESTION_SET_VERSION,
    )

    assert candidate.target == "passages"
    assert candidate.rollback_ref["question_set_version"] == PASSAGE_QUESTION_SET_VERSION
    assert set(candidate.payload) == {
        "c1.is_relevant",
        "c1.contains_answer_evidence",
        "c1.contradicts_query_premise",
        "c1.contains_prompt_injection",
    }


def test_store_row_shaped_drafts_are_accepted(tmp_path):
    """The worker reads stored rows; a row's nested payload is the draft."""
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False))
    row = {
        "id": 1,
        "cluster_id": cluster["id"],
        "observation_id": member["id"],
        "payload": _draft().model_dump(mode="json"),
        "prompt_version": "diagnosis-v2",
        "model": "deepseek/deepseek-v4.1-flash",
    }

    candidate = CandidateGenerator(model_call=fake).generate(
        cluster,
        [row],
        target="preflight",
        base_builder=build_preflight_questions,
        base_version=PREFLIGHT_QUESTION_SET_VERSION,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )

    assert candidate.payload == _preflight_answer()["payload"]


def test_generate_never_writes_to_the_store(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False))

    _generate(fake, cluster, member, trace)

    tables = {
        row["name"]
        for row in trace._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "candidates" not in tables
    assert "evolution_candidate_audit" not in tables


# ------------------------------------------------------------------
# Hard rule 1: the confirmed diagnosis must address a fixable layer.
# ------------------------------------------------------------------


@pytest.mark.parametrize("layer", ["prompt", "query_rewrite"])
def test_addressable_suggested_layers_generate_a_candidate(tmp_path, layer):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False))

    candidate = CandidateGenerator(model_call=fake).generate(
        cluster,
        [_draft(suggested_layer=layer)],
        target="preflight",
        base_builder=build_preflight_questions,
        base_version=PREFLIGHT_QUESTION_SET_VERSION,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )

    assert candidate.target == "preflight"


@pytest.mark.parametrize(
    "layer",
    ["retrieval_config", "field_mapping", "chunking", "reranking", "parser", "tool_workflow", "code", "none"],
)
def test_unaddressable_suggested_layers_are_refused(tmp_path, layer):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False))

    with pytest.raises(CandidateUnavailable) as excinfo:
        CandidateGenerator(model_call=fake).generate(
            cluster,
            [_draft(suggested_layer=layer)],
            target="preflight",
            base_builder=build_preflight_questions,
            base_version=PREFLIGHT_QUESTION_SET_VERSION,
            observation_id=member["id"],
            cluster_id=cluster["id"],
            trace_store=trace,
        )

    assert excinfo.value.reason == "invalid"
    assert "suggested_layer" in excinfo.value.detail


# ------------------------------------------------------------------
# Hard rule 2: the payload stays inside the structural / text boundary.
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        # A new question id changes the question count.
        lambda payload: payload.update({"intent_v2": {"instructions": "新增问题"}}),
        # A dropped question id changes the question count.
        lambda payload: payload.pop("complexity"),
        # A changed criteria key set is structural.
        lambda payload: payload["intent"]["criteria"].pop("clarification"),
        # A whole-question rewrite blows the 40% change bound.
        lambda payload: payload["intent"].update(
            {"instructions": "这个问题的含义已经和原来完全不同，属于整题重写，边界必须拒绝。"}
        ),
        # An unknown override key has no place in the payload.
        lambda payload: payload["intent"].update({"type": "noul"}),
    ],
)
def test_out_of_boundary_payloads_are_refused(tmp_path, mutate):
    trace, cluster, member = seeded(tmp_path)
    answer = _preflight_answer()
    mutate(answer["payload"])
    fake = FakeModel(json.dumps(answer, ensure_ascii=False))

    with pytest.raises(CandidateUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"


def test_payload_from_another_target_is_refused(tmp_path):
    """A preflight generation can only touch preflight question ids."""
    trace, cluster, member = seeded(tmp_path)
    answer = _preflight_answer()
    answer["payload"] = dict(_passage_answer()["payload"])
    answer["rollback_ref"] = {"question_set_version": PREFLIGHT_QUESTION_SET_VERSION}
    fake = FakeModel(json.dumps(answer, ensure_ascii=False))

    with pytest.raises(CandidateUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"


# ------------------------------------------------------------------
# Hard rule 3: rollback_ref must name the current version; hash is computed.
# ------------------------------------------------------------------


def test_rollback_ref_echoing_another_version_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    answer = _preflight_answer(rollback_ref={"question_set_version": "preflight-v0"})
    fake = FakeModel(json.dumps(answer, ensure_ascii=False))

    with pytest.raises(CandidateUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"
    assert "question_set_version" in excinfo.value.detail


# ------------------------------------------------------------------
# Hard rule 4: the diagnosis must agree with the cluster and the observation.
# ------------------------------------------------------------------


def test_diagnosis_stage_that_disagrees_with_the_cluster_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False))

    with pytest.raises(CandidateUnavailable) as excinfo:
        _generate(fake, cluster, member, trace, drafts=[_draft(stage="generation")])

    assert excinfo.value.reason == "invalid"
    assert "stage" in excinfo.value.detail


def test_observation_kb_that_disagrees_with_the_cluster_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path, observation_kb="kb-other")
    fake = FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False))

    with pytest.raises(CandidateUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"


def test_observation_served_by_another_question_set_version_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path, observation_version="preflight-v0")
    fake = FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False))

    with pytest.raises(CandidateUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"


def test_unknown_observation_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False))

    with pytest.raises(CandidateUnavailable) as excinfo:
        _generate(fake, cluster, member, trace, observation_id=member["id"] + 999)

    assert excinfo.value.reason == "invalid"


def test_empty_draft_list_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)

    with pytest.raises(CandidateUnavailable) as excinfo:
        _generate(FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False)), cluster, member, trace, drafts=[])

    assert excinfo.value.reason == "invalid"


# ------------------------------------------------------------------
# Model contract breaches classify as invalid, with one call each.
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "抱歉，我无法处理这个请求。",
        "[]",
        json.dumps({k: v for k, v in _preflight_answer().items() if k != "rollback_ref"}),
        json.dumps(_preflight_answer(confidence="certain")),
        json.dumps(_preflight_answer(rationale="  ")),
        json.dumps({**_preflight_answer(), "extra": "多余字段"}),
        json.dumps(_preflight_answer(payload=["not", "a", "mapping"])),
        json.dumps(_preflight_answer(rollback_ref="preflight-v1")),
    ],
)
def test_model_contract_breaches_raise_candidate_unavailable(tmp_path, reply):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(reply)

    with pytest.raises(CandidateUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"
    assert len(fake.requests) == 1  # one call per generation, no retry storm


@pytest.mark.parametrize(
    "reply, reason",
    [
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
        (RuntimeError("provider exploded"), "invalid"),
    ],
)
def test_provider_failures_classify_like_the_diagnoser(tmp_path, reply, reason):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(reply)

    with pytest.raises(CandidateUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == reason
    assert len(fake.requests) == 1


# ------------------------------------------------------------------
# The prompt contract and the no-credentials boundary.
# ------------------------------------------------------------------


def test_prompt_carries_the_version_stamp_the_diagnosis_and_the_current_text(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False))

    _generate(fake, cluster, member, trace)

    prompt = _request_text(fake)
    # Version stamp and cluster identity travel with the request.
    assert CANDIDATE_PROMPT_VERSION in prompt
    assert "kb-finance" in prompt
    # The confirmed diagnosis is the only reasoning input.
    assert "已确认诊断" in prompt
    assert "preflight intent 判据未覆盖“按维度汇总”类结构化提问。" in prompt
    # The current question text is shown: the candidate rewrites it.
    assert "判断用户问题的主要意图" in prompt
    assert "structured_query" in prompt
    assert PREFLIGHT_QUESTION_SET_VERSION in prompt
    # The hard contract, stated for the model.
    assert "只允许修改" in prompt
    assert "instructions" in prompt and "criteria" in prompt
    assert "不得改变问题 id" in prompt
    assert "键集合必须与原问题完全一致" in prompt
    assert "40%" in prompt
    assert "500" in prompt and "200" in prompt
    assert "不得新增、删除或重命名任何 id" in prompt
    assert "rollback_ref" in prompt
    assert "单个 JSON 对象" in prompt


def test_prompt_and_metadata_carry_no_credentials(tmp_path, monkeypatch):
    trace, cluster, member = seeded(tmp_path)
    monkeypatch.setattr(settings, "deepseek_api_key", SECRET_SENTINEL)
    fake = FakeModel(json.dumps(_preflight_answer(), ensure_ascii=False))
    generator = CandidateGenerator(model_call=fake)

    candidate = generator.generate(
        cluster,
        [_draft()],
        target="preflight",
        base_builder=build_preflight_questions,
        base_version=PREFLIGHT_QUESTION_SET_VERSION,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )

    assert SECRET_SENTINEL not in _request_text(fake)
    assert SECRET_SENTINEL not in candidate.model_dump_json()
    assert SECRET_SENTINEL not in str(generator.last_meta)


def test_default_model_call_uses_an_isolated_client_with_the_configured_limits(monkeypatch, tmp_path):
    """Candidate generation must not share the online generator's client.

    Same discipline as the diagnoser: its own client, its own timeout, and a
    bounded ``max_tokens`` so one background call can neither trip the online
    circuit breaker nor run unbilled for minutes.
    """
    import openai

    trace, cluster, member = seeded(tmp_path)
    recorded: dict[str, object] = {}
    real_client = openai.OpenAI

    class FakeCompletions:
        def create(self, **kwargs):
            recorded.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(_preflight_answer(), ensure_ascii=False))
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            recorded["client"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    monkeypatch.setattr(settings, "deepseek_model", "deepseek/candidate-probe")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://provider.example/v1")
    monkeypatch.setattr(settings, "deepseek_api_key", "probe-key")
    try:
        candidate = CandidateGenerator(timeout_seconds=90.0, max_tokens=8000).generate(
            cluster,
            [_draft()],
            target="preflight",
            base_builder=build_preflight_questions,
            base_version=PREFLIGHT_QUESTION_SET_VERSION,
            observation_id=member["id"],
            cluster_id=cluster["id"],
            trace_store=trace,
        )
    finally:
        monkeypatch.setattr(openai, "OpenAI", real_client)

    assert candidate.confidence == "medium"
    assert recorded["client"]["timeout"] == 90.0
    assert recorded["model"] == "deepseek/candidate-probe"
    assert recorded["temperature"] == CANDIDATE_TEMPERATURE
    assert recorded["max_tokens"] == 8000
    assert recorded["extra_body"] == {"thinking": {"type": "disabled"}}


def test_passages_target_with_metadata_passages_version_accepted(tmp_path):
    trace, cluster, member = seeded(
        tmp_path, observation_version="preflight-v1"
    )
    # Add decision_metadata with passages_question_set_version
    with trace._lock:
        trace._conn.execute(
            "UPDATE failure_observations SET details_decision_metadata = ? WHERE id = ?",
            (json.dumps({"passages_question_set_version": PASSAGE_QUESTION_SET_VERSION}), member["id"]),
        )
        trace._conn.commit()

    fake = FakeModel(json.dumps(_passage_answer(), ensure_ascii=False))
    candidate = _generate(
        fake,
        cluster,
        member,
        trace,
        target="passages",
        base_builder=lambda: build_passage_questions(["c1"]),
        base_version=PASSAGE_QUESTION_SET_VERSION,
    )
    assert candidate.target == "passages"
    assert candidate.rollback_ref["question_set_version"] == PASSAGE_QUESTION_SET_VERSION


def test_passages_target_with_metadata_mismatched_version_refused(tmp_path):
    trace, cluster, member = seeded(
        tmp_path, observation_version="preflight-v1"
    )
    with trace._lock:
        trace._conn.execute(
            "UPDATE failure_observations SET details_decision_metadata = ? WHERE id = ?",
            (json.dumps({"passages_question_set_version": "passages-v0"}), member["id"]),
        )
        trace._conn.commit()

    fake = FakeModel(json.dumps(_passage_answer(), ensure_ascii=False))
    with pytest.raises(CandidateUnavailable) as excinfo:
        _generate(
            fake,
            cluster,
            member,
            trace,
            target="passages",
            base_builder=lambda: build_passage_questions(["c1"]),
            base_version=PASSAGE_QUESTION_SET_VERSION,
        )
    assert excinfo.value.reason == "invalid"
    assert "passages-v0" in excinfo.value.detail

