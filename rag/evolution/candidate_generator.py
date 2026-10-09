"""Constrained generation of question-set candidates (phase 4, Task 2).

One confirmed diagnosis becomes at most one ``jev_question_set`` candidate
through exactly one model call. The input is already human-approved (design
section 5: the cluster reached ``approved_for_candidate`` and its diagnosis
draft was administrator-confirmed), so the generator's job is narrow: turn the
diagnosis plus the **current** question-set text into a small, bounded text
variant that a later gate layer can compare question by question.

The prompt is versioned (:data:`CANDIDATE_PROMPT_VERSION`) and its contract —
rewrite ``instructions``/``criteria`` text only, never question ids, counts or
primitive types, echo the current question-set version in ``rollback_ref``,
emit one JSON object — is enforced twice: in the prompt text and again in the
hard post-validation rules of :func:`_enforce_candidate_rules`. A candidate is
never repaired on failure: every breach raises :class:`CandidateUnavailable`,
so an observation either yields a fully compliant candidate or nothing.

The model channel mirrors the phase-3 diagnoser exactly (same
``settings.deepseek_*`` provider settings, isolated client, bounded timeout and
``max_tokens``, thinking disabled, same failure classification), because a
background generation call must never trip the online generator's circuit
breaker and must stay inside the worker's call budget. The two small helpers
that shape that channel (:func:`_default_model_call` and
:func:`_classify_failure`) are distilled copies of the diagnoser's private
helpers: ``rag/evolution/diagnoser.py`` is owned by another lane and must not
be edited here, and importing its private names across modules would be worse
than a documented mirror. When a shared module becomes possible, both should
collapse onto it; the failure ``reason`` strings are already shared through the
diagnoser's public constants.

No secrets leave this module: the request, the recorded call metadata
(:attr:`CandidateGenerator.last_meta`) and the failure ``detail`` carry field
names, ids, lengths and exception classes only — never question text, model
output or credentials. Persistence is the caller's job: :meth:`generate` reads
the observation for its consistency checks and writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from typing import get_args

import openai
from pydantic import ValidationError

import rag.generator as generator
from config import settings
from rag.evolution.candidates import (
    ARTIFACT_TYPE,
    TARGETS,
    JevQuestionSetCandidate,
    PayloadBoundaryError,
    validate_payload,
)
from rag.evolution.diagnoser import REASON_INVALID, REASON_RATE_LIMITED, REASON_TIMEOUT
from rag.evolution.models import DiagnosisDraft

#: Version of the candidate prompt; stored next to every candidate so a prompt
#: revision can always be traced back (change the prompt, bump this).
CANDIDATE_PROMPT_VERSION = "candidate-v1"

type ModelCall = Callable[[list[dict]], str]

#: Candidate temperature: low, close to deterministic, but not greedy — the same
#: discipline as the diagnosis prompt.
CANDIDATE_TEMPERATURE = 0.2

#: Diagnosis layers a ``jev_question_set`` candidate can address (design
#: section 6 suggestion layers): the judgement prompt itself and the
#: query-rewrite / intent calibration it feeds. Every other layer belongs to
#: another phase and must be refused here.
ADDRESSABLE_LAYERS: tuple[str, ...] = ("prompt", "query_rewrite")

#: The one JSON object the model must emit — nothing else, nothing missing.
_ANSWER_FIELDS: tuple[str, ...] = (
    "payload",
    "rationale",
    "expected_effect",
    "confidence",
    "rollback_ref",
)

#: The confirmed diagnosis fields rendered into the prompt and checked here.
_DRAFT_FIELDS: tuple[str, ...] = tuple(DiagnosisDraft.model_fields)
_CONFIDENCES: tuple[str, ...] = get_args(DiagnosisDraft.model_fields["confidence"].annotation)

_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, openai.APITimeoutError)
_RATE_LIMIT_ERRORS: tuple[type[BaseException], ...] = (openai.RateLimitError,)


class CandidateUnavailable(RuntimeError):
    """One confirmed diagnosis could not be turned into a candidate.

    ``reason`` classifies the failure for the worker round: ``timeout`` and
    ``rate_limited`` are capacity signals worth another round, while ``invalid``
    means either the model answer broke the prompt contract or the answer
    stepped outside the candidate boundary. ``detail`` names failing fields,
    question ids, lengths and exception classes only — it never carries
    credentials, question text or raw model output.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _classify_failure(exc: BaseException) -> tuple[str, str]:
    """Map a provider/transport failure onto a worker-level reason + detail.

    Distilled mirror of ``rag.evolution.diagnoser._classify`` (owned by another
    lane); the classification vocabulary is shared through the diagnoser's
    public reason constants. Any unrecognized failure is ``invalid`` rather
    than a silent pass: an unknown provider error must not look like a
    capacity signal the worker should retry.
    """
    if isinstance(exc, CandidateUnavailable):
        return exc.reason, exc.detail
    if isinstance(exc, generator.StructuredOutputError):
        return REASON_INVALID, "structured_output_invalid"
    if isinstance(exc, _TIMEOUT_ERRORS):
        return REASON_TIMEOUT, type(exc).__name__
    if isinstance(exc, _RATE_LIMIT_ERRORS):
        return REASON_RATE_LIMITED, type(exc).__name__
    if isinstance(exc, openai.APIStatusError) and exc.status_code == 429:
        return REASON_RATE_LIMITED, "http_429"
    return REASON_INVALID, type(exc).__name__


def _default_model_call(timeout_seconds: float = 90.0, max_tokens: int = 8000) -> ModelCall:
    """Build an isolated model call for candidate generation at low temperature.

    Same discipline as the diagnoser's default call, and for the same reasons:
    the call reuses the generator's provider settings and auth-header helper but
    owns its client, so a background generation timeout can never trip the
    online generator's circuit breaker; ``max_tokens`` bounds the completion
    because this provider's reasoning tokens count against the budget and an
    uncapped call can run for minutes.
    """

    def call(messages: list[dict]) -> str:
        client = openai.OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=timeout_seconds,
            default_headers=generator.deepseek_default_headers(),
        )
        clean_msgs = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]
        response = client.chat.completions.create(
            model=settings.deepseek_model,
            messages=clean_msgs,
            temperature=CANDIDATE_TEMPERATURE,
            max_tokens=max_tokens,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return response.choices[0].message.content or "（模型未返回内容）"

    return call


# ------------------------------------------------------------------
# Prompt assembly.
# ------------------------------------------------------------------


def _compact(value: object) -> str:
    """Deterministic, JSON-safe rendering of one diagnosis field."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _render_cluster(cluster: Mapping) -> str:
    """Cluster header: identity only — the same signal set the diagnoser sees."""
    keys = ("id", "kb_id", "failure_stage", "priority", "member_count", "pool", "status")
    return "\n".join(f"{key}: {cluster.get(key)}" for key in keys)


def _render_drafts(drafts: tuple[dict, ...]) -> str:
    """Render the confirmed diagnosis drafts the candidate must serve."""
    blocks = []
    for index, fields in enumerate(drafts, start=1):
        lines = [f"[诊断草案 {index}/{len(drafts)}]"]
        lines.extend(f"{name}: {_compact(fields.get(name))}" for name in _DRAFT_FIELDS)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _question_text(question: object) -> tuple[str, dict[str, str], str]:
    """Read ``(instructions, criteria, primitive type)`` off a base question."""
    if isinstance(question, Mapping):
        instructions = question.get("instructions") or ""
        criteria = question.get("criteria") or {}
        kind = str(question.get("type") or "")
    else:
        instructions = getattr(question, "instructions", "") or ""
        criteria = getattr(question, "criteria", {}) or {}
        kind = str(getattr(question, "type", "") or type(question).__name__)
    return str(instructions), {str(k): str(v) for k, v in criteria.items()}, kind


def _render_question_set(base: Mapping[str, object]) -> str:
    """Render the current question text the candidate is allowed to rewrite.

    The full text appears here by design: a question-set candidate is a rewrite
    of exactly this text, so the model cannot be asked to produce one without
    seeing it. Everything else about the set (ids, count, primitive type) is
    structural and stays fixed.
    """
    lines: list[str] = []
    for question_id, question in base.items():
        instructions, criteria, kind = _question_text(question)
        lines.append(f"- id: {question_id}")
        lines.append(f"  type: {kind or 'unknown'}")
        lines.append(f"  instructions: {instructions}")
        if criteria:
            lines.append("  criteria:")
            lines.extend(f"    {label}: {text}" for label, text in criteria.items())
        else:
            lines.append("  criteria: （无）")
    return "\n".join(lines)


def _build_messages(
    cluster: Mapping,
    drafts: tuple[dict, ...],
    target: str,
    base: Mapping[str, object],
    base_version: str,
    prompt_version: str,
) -> list[dict]:
    """Assemble the versioned two-message candidate request.

    The hard contract is stated before the call (rules 1–8) so the model knows
    the boundary it must stay inside, and repeated after it in
    :func:`_enforce_candidate_rules` so a breach cannot become a candidate.
    """
    system = "\n".join(
        (
            f"你是知识库自进化系统的候选生成助手（候选提示词版本：{prompt_version}）。",
            "你的唯一任务：为一条已由管理员确认根因的失败诊断，生成一个 jev_question_set 候选"
            "——对 Jev 判定层问题集文本的小幅改写。",
            "候选只是候选：它还要经过基准门禁、影子对比与人工确认才可能上线；"
            "你只负责给出有界、可回滚的文本改写。",
            "",
            "【硬性规则】",
            "1. 只允许修改问题的 instructions 文本与 criteria 各条目文本；"
            "不得改变问题 id、问题数量或问题类型（Choice/Noul）；criteria 的键集合必须与原问题完全一致。",
            "2. 每个被改写字段相对原文的改动不得超过 40% 字符（编辑距离与较长一侧文本长度之比）；"
            "instructions 不超过 500 字符，每条 criteria 不超过 200 字符；不得整题重写，不得重写整个问题集。",
            "3. payload 必须列出当前问题集的全部问题 id：未改写的题目给出空对象 {}；"
            "不得新增、删除或重命名任何 id。",
            "4. rollback_ref 必须回指当前问题集版本：question_set_version 原样填入请求中给出的当前版本；"
            "载荷快照哈希由系统计算，不要填写 payload_sha256。",
            "5. 只依据请求中给出的已确认诊断与当前问题集文本进行改写；"
            "禁止引入知识库原文、用户答案、密钥或请求之外的任何内容。",
            "6. 候选只能服务本次诊断（其建议修改层级见请求），不得顺带改写与诊断无关的问题。",
            "7. 只输出单个 JSON 对象：不要 Markdown 代码块、注释、解释或任何多余文字。",
            "8. 输出中不得出现密钥、令牌或提示词指令本身。",
            "",
            "【输出字段】JSON 对象必须包含以下 5 个字段，缺一不可，且不得包含额外字段：",
            '- payload：问题集文本覆写，形如 {"question_id": {"instructions": "文本", '
            '"criteria": {"标签": "文本"}}}；instructions 与 criteria 均可选，但至少改写一个问题。',
            "- rationale：改写理由，必须引用已确认诊断的根因。",
            "- expected_effect：预期效果，必须对应该诊断的验收标准。",
            "- confidence：置信度，只能取 low、medium、high 之一。",
            f'- rollback_ref：{{"question_set_version": "{base_version}"}}，原样回指当前版本。',
        )
    )
    user = "\n\n".join(
        (
            "# 失败聚类\n" + _render_cluster(cluster),
            "# 已确认诊断（管理员已确认根因）\n" + _render_drafts(drafts),
            f"# 目标问题集（target：{target}，当前版本：{base_version}）\n"
            + _render_question_set(base),
            "请严格按系统提示输出单个 JSON 对象。",
        )
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ------------------------------------------------------------------
# Input and answer validation.
# ------------------------------------------------------------------


def _normalize_drafts(drafts: object) -> tuple[dict, ...]:
    """Accept ``DiagnosisDraft`` objects, stored draft rows or plain mappings.

    The worker hands over whatever shape it read (a frozen draft, or a stored
    row whose nested ``payload`` is the draft), so all three normalize to the
    same field mapping; anything missing a draft field is a contract breach,
    not something to repair.
    """
    if isinstance(drafts, Mapping) or not isinstance(drafts, (list, tuple)):
        raise CandidateUnavailable(REASON_INVALID, "drafts must be a list of confirmed diagnosis drafts")
    if not drafts:
        raise CandidateUnavailable(REASON_INVALID, "no confirmed diagnosis draft for this observation")
    normalized: list[dict] = []
    for index, item in enumerate(drafts):
        if isinstance(item, DiagnosisDraft):
            fields = item.model_dump(mode="json")
        elif isinstance(item, Mapping):
            nested = item.get("payload")
            fields = dict(nested) if isinstance(nested, Mapping) else dict(item)
        else:
            raise CandidateUnavailable(REASON_INVALID, f"draft[{index}] is not a diagnosis draft")
        missing = [name for name in _DRAFT_FIELDS if name not in fields]
        if missing:
            raise CandidateUnavailable(
                REASON_INVALID, f"draft[{index}] is missing fields: {', '.join(missing)}"
            )
        normalized.append(fields)
    return tuple(normalized)


def _validate_inputs(
    cluster: object,
    target: object,
    base: object,
    base_version: object,
    cluster_id: int,
) -> None:
    """Reject an unusable request before spending a model call on it.

    Only the pieces the prompt itself is built from are checked here; the four
    hard candidate rules stay in :func:`_enforce_candidate_rules`.
    """
    if not isinstance(cluster, Mapping):
        raise CandidateUnavailable(REASON_INVALID, "cluster must be a mapping")
    cluster_row_id = cluster.get("id")
    if cluster_row_id is not None and int(cluster_row_id) != int(cluster_id):
        raise CandidateUnavailable(REASON_INVALID, "cluster_id disagrees with the cluster row")
    if target not in TARGETS:
        raise CandidateUnavailable(REASON_INVALID, f"unknown candidate target: {target!r}")
    if not isinstance(base_version, str) or not base_version.strip():
        raise CandidateUnavailable(REASON_INVALID, "base_version must be a non-empty string")
    if not isinstance(base, Mapping) or not base:
        raise CandidateUnavailable(REASON_INVALID, "base question set is empty")


def _validate_completion(text: str, base_version: str) -> dict:
    """One JSON object, exactly the declared fields, version echo included."""
    try:
        answer = generator._extract_json_object(text)
    except ValueError:
        raise CandidateUnavailable(REASON_INVALID, "response is not a JSON object") from None
    except generator.StructuredOutputError:
        raise CandidateUnavailable(REASON_INVALID, "structured_output_invalid") from None
    if not isinstance(answer, dict):
        raise CandidateUnavailable(REASON_INVALID, "response is not a JSON object")
    missing = [name for name in _ANSWER_FIELDS if name not in answer]
    if missing:
        raise CandidateUnavailable(REASON_INVALID, f"missing fields: {', '.join(missing)}")
    unexpected = sorted(set(answer) - set(_ANSWER_FIELDS))
    if unexpected:
        raise CandidateUnavailable(REASON_INVALID, f"unexpected fields: {', '.join(unexpected)}")
    for name in ("rationale", "expected_effect"):
        value = answer[name]
        if not isinstance(value, str) or not value.strip():
            raise CandidateUnavailable(REASON_INVALID, f"{name} must be a non-empty string")
    if answer["confidence"] not in _CONFIDENCES:
        raise CandidateUnavailable(REASON_INVALID, "confidence must be one of low/medium/high")
    if not isinstance(answer["payload"], dict):
        raise CandidateUnavailable(REASON_INVALID, "payload must map question ids to overrides")
    rollback = answer["rollback_ref"]
    if not isinstance(rollback, Mapping):
        raise CandidateUnavailable(REASON_INVALID, "rollback_ref must be a mapping")
    version = rollback.get("question_set_version")
    if not isinstance(version, str) or not version.strip():
        raise CandidateUnavailable(
            REASON_INVALID, "rollback_ref must name the current question_set_version"
        )
    if version != base_version:
        raise CandidateUnavailable(
            REASON_INVALID, "rollback_ref names a different question_set_version"
        )
    return answer


def _payload_sha256(payload: Mapping[str, object]) -> str:
    """Digest of the canonical payload: sorted-key, compact, UTF-8 JSON.

    A rollback restores exactly the payload snapshot this digest names, so the
    serialization must be deterministic across processes (design section 13:
    rollback depends on the stored record alone).
    """
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------
# The hard post-validation rules.
# ------------------------------------------------------------------


def _enforce_diagnosis_scope(
    drafts: tuple[dict, ...],
    cluster: Mapping,
    observation_id: int,
    base_version: str,
    trace_store,
) -> None:
    """Rule 4: the diagnosis, the cluster and the observation must agree.

    * every draft's ``stage`` matches the cluster's authoritative
      ``failure_stage`` (a diagnosis of another stage cannot justify editing
      this cluster's question set);
    * the member observation's ``kb_id`` matches the cluster's, so a candidate
      is never generated across knowledge bases;
    * when the observation records the question-set version that served the
      failed request, it is the version being edited — otherwise the confirmed
      diagnosis describes behaviour of a set the candidate does not touch.
    """
    cluster_stage = str(cluster.get("failure_stage") or "").strip()
    cluster_kb = str(cluster.get("kb_id") or "").strip()
    try:
        observation = trace_store.get_observation(observation_id)
    except Exception as exc:
        raise CandidateUnavailable(
            REASON_INVALID, f"observation read failed: {type(exc).__name__}"
        ) from exc
    if observation is None:
        raise CandidateUnavailable(REASON_INVALID, f"observation {observation_id} not found")
    observation_kb = str(observation.get("kb_id") or "").strip()
    if cluster_kb and observation_kb and observation_kb != cluster_kb:
        raise CandidateUnavailable(REASON_INVALID, "diagnosis kb disagrees with the cluster kb")
    served_version = str(observation.get("details_jev_question_set_version") or "").strip()
    if served_version and served_version != base_version:
        raise CandidateUnavailable(
            REASON_INVALID,
            "observation was served by question_set_version "
            f"{served_version!r}, not the current {base_version!r}",
        )
    for index, fields in enumerate(drafts):
        stage = str(fields.get("stage") or "").strip()
        if cluster_stage and stage and stage.lower() != cluster_stage.lower():
            raise CandidateUnavailable(
                REASON_INVALID,
                f"draft[{index}].stage {stage!r} disagrees with the cluster "
                f"failure_stage {cluster_stage!r}",
            )


def _enforce_candidate_rules(
    answer: dict,
    *,
    target: str,
    base: Mapping[str, object],
    drafts: tuple[dict, ...],
    cluster: Mapping,
    base_version: str,
    observation_id: int,
    trace_store,
) -> JevQuestionSetCandidate:
    """Apply the four hard rules; return the candidate or refuse it whole.

    1. the confirmed diagnosis addresses a layer a question-set text variant
       can fix (``suggested_layer`` in :data:`ADDRESSABLE_LAYERS`);
    2. the payload stays inside the boundary of this target's current set
       (:func:`~rag.evolution.candidates.validate_payload`: ids, count,
       criteria keys, lengths and the ≤40% per-field change bound);
    3. ``rollback_ref`` names the current version and hashes the payload
       snapshot (the version echo is checked in :func:`_validate_completion`,
       the digest is computed here from the canonical payload);
    4. the diagnosis agrees with the cluster and the observation on stage, kb
       and served question-set version, and the payload only touches this
       target's base questions — which rule 2's id-set equality guarantees.
    """
    for index, fields in enumerate(drafts):
        layer = fields.get("suggested_layer")
        if layer not in ADDRESSABLE_LAYERS:
            raise CandidateUnavailable(
                REASON_INVALID,
                f"draft[{index}].suggested_layer {layer!r} is not addressable by a "
                "jev_question_set candidate",
            )
    payload = answer["payload"]
    try:
        validate_payload(target, payload, base)
    except PayloadBoundaryError as error:
        raise CandidateUnavailable(REASON_INVALID, error.reason) from None
    _enforce_diagnosis_scope(drafts, cluster, observation_id, base_version, trace_store)
    rollback_ref = {
        "question_set_version": base_version,
        "payload_sha256": _payload_sha256(payload),
    }
    try:
        return JevQuestionSetCandidate(
            artifact_type=ARTIFACT_TYPE,
            target=target,
            payload=payload,
            rationale=answer["rationale"],
            expected_effect=answer["expected_effect"],
            rollback_ref=rollback_ref,
            confidence=answer["confidence"],
        )
    except ValidationError as exc:
        locations = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
        raise CandidateUnavailable(REASON_INVALID, f"candidate contract: {locations}") from None


class CandidateGenerator:
    """One versioned, bounded candidate generation call per confirmed diagnosis."""

    def __init__(
        self,
        model_call: ModelCall | None = None,
        *,
        prompt_version: str = CANDIDATE_PROMPT_VERSION,
        timeout_seconds: float = 90.0,
        max_tokens: int = 8000,
    ) -> None:
        self._model_call: ModelCall = model_call or _default_model_call(
            timeout_seconds, max_tokens
        )
        #: Prompt version of this instance; persisted next to every candidate.
        self.prompt_version = prompt_version
        #: Model label for the candidate row (Task 1 store ``model`` column).
        self.model_name = settings.deepseek_model
        #: Cost of the last call: prompt version, model, elapsed ms, output size.
        self.last_meta: dict[str, object] | None = None

    def generate(
        self,
        cluster: dict,
        drafts: list,
        *,
        target: str,
        base_builder: Callable[[], dict],
        base_version: str,
        observation_id: int,
        cluster_id: int,
        trace_store,
    ) -> JevQuestionSetCandidate:
        """Generate one candidate for a confirmed diagnosis, or refuse.

        ``cluster`` is the approved cluster row and ``drafts`` the
        administrator-confirmed diagnosis drafts of ``observation_id``.
        ``base_builder()`` returns the target's current question set (the
        ``rag.jev.questions`` builder in production) and ``base_version`` names
        it; ``trace_store`` is read — never written — for the observation's
        consistency signals. Returns a validated candidate or raises
        :class:`CandidateUnavailable`; persistence is the caller's job.
        """
        normalized = _normalize_drafts(drafts)
        try:
            base = base_builder()
        except Exception as exc:
            reason, detail = _classify_failure(exc)
            raise CandidateUnavailable(reason, detail) from exc
        _validate_inputs(cluster, target, base, base_version, cluster_id)

        messages = _build_messages(cluster, normalized, target, base, base_version, self.prompt_version)
        started = time.perf_counter()
        completion_chars = 0
        try:
            completion = self._model_call(messages)
            completion_chars = len(completion or "")
            answer = _validate_completion(completion, base_version)
            candidate = _enforce_candidate_rules(
                answer,
                target=target,
                base=base,
                drafts=normalized,
                cluster=cluster,
                base_version=base_version,
                observation_id=observation_id,
                trace_store=trace_store,
            )
        except CandidateUnavailable:
            self._record(started, completion_chars)
            raise
        except Exception as exc:
            reason, detail = _classify_failure(exc)
            self._record(started, completion_chars)
            raise CandidateUnavailable(reason, detail) from exc
        self._record(started, completion_chars)
        return candidate

    def _record(self, started: float, completion_chars: int) -> None:
        """Record call cost without storing prompt or completion text."""
        self.last_meta = {
            "prompt_version": self.prompt_version,
            "model": self.model_name,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "completion_chars": completion_chars,
        }


__all__ = [
    "ADDRESSABLE_LAYERS",
    "CANDIDATE_PROMPT_VERSION",
    "CANDIDATE_TEMPERATURE",
    "CandidateGenerator",
    "CandidateUnavailable",
]
