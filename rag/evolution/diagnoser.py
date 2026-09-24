"""Structured diagnosis drafts for candidate clusters (phase 3, design section 6).

One cluster means exactly one model call, on top of the deterministic-first
:class:`~rag.evolution.models.EvidencePack` assembled by the evidence step. The
prompt is versioned (:data:`DIAGNOSIS_PROMPT_VERSION`) and its contract — reason
only from the pack, use the design-section-6 taxonomy, reference pack-internal
hashes/labels only, emit one JSON object with every field — is enforced twice:
in the prompt text and again in :func:`_enforce_evidence_rules`. A draft is
never a confirmed conclusion, so every failure path raises
:class:`DiagnosisUnavailable` rather than returning a half-validated draft.

Design rules carried here (design sections 5/6/9 and the plan's Global
Constraints):

* without admin-confirmed labels the root cause can only be hypothesis-level:
  ``confidence == "low"`` and the root cause says "假设";
* a failed deterministic reproduction check keeps the cluster in the
  observation queue: ``confidence == "low"``, the root cause says "待复核" and
  recommends ``needs_more_evidence``, and the draft flags ``reproducible``
  exactly as the pack's check did;
* the prompt, the request, the draft and the recorded metadata never carry
  credentials, raw model output or unauthorized content;
* the per-round call budget belongs to the worker (Task 4): this module only
  classifies failures so the worker can decide whether to retry later.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import get_args

import openai
from pydantic import ValidationError

import rag.generator as generator
from config import settings
from rag.evolution.models import DiagnosisDraft, EvidencePack

#: Version of the diagnosis prompt; stored next to every draft so a prompt
#: revision can always be traced back (change the prompt, bump this).
#: diagnosis-v2: the cluster header no longer renders the recorder's
#: root_cause_feature (it biased every draft toward that label), and the evidence
#: pack renders bounded chunk previews plus an explicit recall/ranking/content-gap
#: classification rule.
DIAGNOSIS_PROMPT_VERSION = "diagnosis-v2"

type ModelCall = Callable[[list[dict]], str]

#: Diagnosis temperature: low, close to deterministic, but not greedy.
DIAGNOSIS_TEMPERATURE = 0.2

REASON_INVALID = "invalid"
REASON_TIMEOUT = "timeout"
REASON_RATE_LIMITED = "rate_limited"
REASON_BUDGET = "budget"

_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, openai.APITimeoutError)
_RATE_LIMIT_ERRORS: tuple[type[BaseException], ...] = (openai.RateLimitError,)


class DiagnosisUnavailable(RuntimeError):
    """One cluster could not be diagnosed; no draft was produced for it.

    ``reason`` classifies the failure for the worker round: ``timeout``,
    ``rate_limited`` and ``budget`` are capacity signals worth another round,
    while ``invalid`` means the model output did not honour the prompt
    contract. ``detail`` names failing fields or exception classes only — it
    never carries credentials or raw model text.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _literal_values(field_name: str) -> tuple[str, ...]:
    """Allowed values of a ``Literal`` draft field, read from the contract."""
    annotation = DiagnosisDraft.model_fields[field_name].annotation
    values = get_args(annotation)
    if not values:
        raise RuntimeError(f"draft field {field_name} is not a Literal")
    return values


_FAILURE_CATEGORIES = _literal_values("failure_category")
_SUGGESTED_LAYERS = _literal_values("suggested_layer")
_CONFIDENCES = _literal_values("confidence")
_DRAFT_FIELDS = tuple(DiagnosisDraft.model_fields)

_FIELD_SPEC = (
    "- failure_category：失败类别，只能取上面给出的枚举值。",
    "- stage：发生阶段，必须与证据包的 failure_stage 一致。",
    "- first_divergence：失败轨迹相对参考证据的第一处实质偏离。",
    "- direct_evidence：直接证据，只引用证据包内的块哈希或管理员标签。",
    "- root_cause：根因；证据不足时必须标注为假设级。",
    "- impact_scope：影响范围（受影响的问题类型与知识库）。",
    "- suggested_layer：建议修改层级，只能取上面给出的枚举值。",
    "- reproduction_steps：复现方法，如何确定性地重放这个失败。",
    "- acceptance_criteria：验收标准，修复后如何判定通过。",
    "- confidence：置信度，只能取上面给出的枚举值。",
    "- reproducible：是否可复现，必须与证据包的复现结论一致。",
    "- evidence_refs：引用证据，证据包内哈希或标签组成的字符串数组。",
)


def _default_model_call(
    timeout_seconds: float = 30.0, max_tokens: int = 8000
) -> ModelCall:
    """Build an isolated model call for diagnosis at a low temperature.

    The call reuses the generator's provider settings and auth-header helper but
    owns its client: a background diagnosis timeout must never trip the online
    generator's circuit breaker, and the diagnosis timeout belongs to the worker
    configuration (``evolution_diagnosis_timeout_seconds``). ``max_tokens`` bounds
    the completion: this provider's reasoning tokens count against the budget and
    an uncapped call can run for minutes, while a small cap starves the JSON.
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
            temperature=DIAGNOSIS_TEMPERATURE,
            max_tokens=max_tokens,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return response.choices[0].message.content or "（模型未返回内容）"

    return call


def _classify(exc: BaseException) -> tuple[str, str]:
    """Map a provider/transport failure onto a worker-level reason + detail."""
    if isinstance(exc, DiagnosisUnavailable):
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


def _compact(value: object) -> str:
    """Deterministic, ASCII-safe (JSON-safe) rendering of one evidence entry."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _render_cluster(cluster: dict) -> str:
    """Cluster header: identity only — deliberately WITHOUT the recorder's feature.

    Rendering ``root_cause_feature`` here made the model echo the recorder's own
    label instead of diagnosing from the evidence; the first dev-set run showed
    exactly that failure mode (every draft was ``recall_miss``, the recorder's
    feature for this corpus). The feature is metadata, not evidence.
    """
    keys = ("id", "kb_id", "failure_stage", "priority", "member_count", "pool", "status")
    return "\n".join(f"{key}: {cluster.get(key)}" for key in keys)


def _render_evidence_pack(pack: EvidencePack) -> str:
    """Render the pack the model may reason from: hashes, previews, labels, verdicts.

    ``recall_previews`` carry bounded leading text of the *current* re-retrieval
    (not the failure-time text; versioned corpora may differ) so the model can
    tell "the retrieved chunks do not contain the answer" from "the needed chunk
    was never retrieved" — hash-only evidence cannot distinguish those.
    """
    lines = ["（以下只包含块哈希、块预览、标签与结论；块预览来自当前重检索，不是失败当时的原文）"]
    lines.append("kb_id: " + pack.kb_id)
    lines.append("failure_stage: " + pack.failure_stage)
    lines.append("question_samples:")
    for question in pack.question_samples:
        lines.append(f"  - {question}")
    if not pack.question_samples:
        lines.append("  - （无）")
    lines.append("admin_labels:")
    if pack.admin_labels:
        lines.extend(f"  - {_compact(label)}" for label in pack.admin_labels)
    else:
        lines.append("  - （无管理员确认标签）")
    lines.append("success_traces:")
    if pack.success_traces:
        lines.extend(f"  - {_compact(trace)}" for trace in pack.success_traces)
    else:
        lines.append("  - （无）")
    lines.append("recall_hashes: " + (", ".join(pack.recall_hashes) or "（无）"))
    lines.append("failed_hashes: " + (", ".join(pack.failed_hashes) or "（无）"))
    lines.append("recall_previews（当前重检索的块预览，用于判断内容是否在召回块中）:")
    if pack.recall_previews:
        for preview in pack.recall_previews:
            lines.append(
                f"  - doc={preview.get('doc')} hash={preview.get('hash')} text={preview.get('text')}"
            )
    else:
        lines.append("  - （无）")
    lines.append("reproduction.reproducible: " + str(bool(pack.reproduction.reproducible)).lower())
    lines.append("reproduction.missing_hashes: " + (", ".join(pack.reproduction.missing_hashes) or "（无）"))
    if pack.reproduction.note:
        lines.append("reproduction.note: " + pack.reproduction.note)
    return "\n".join(lines)


def _conditional_rules(pack: EvidencePack) -> tuple[str, ...]:
    """Pack-specific rules the answer must obey, stated before the call."""
    rules = []
    if pack.reproduction.reproducible:
        rules.append("本次复现检查通过：字段 reproducible 必须为 true。")
    else:
        rules.append(
            "本次复现检查未通过：字段 reproducible 必须为 false；confidence 只能为 low；"
            "root_cause 必须包含“待复核”字样，并建议管理员执行 needs_more_evidence"
            "（补充证据后重新诊断）；reproduction_steps 必须说明无法复现的原因。"
        )
    if pack.admin_labels:
        rules.append(
            "证据包含管理员确认标签，确定性证据优先：可以给出 medium/high 置信度结论，"
            "但必须引用具体标签，且 direct_evidence 与 evidence_refs 只能引用证据包内的哈希或标签。"
        )
    else:
        rules.append(
            "证据包中没有任何管理员确认标签：confidence 只能为 low，且 root_cause "
            "必须包含“假设”字样、明确标注这是假设级根因。"
        )
    return tuple(rules)


def _build_messages(cluster: dict, pack: EvidencePack, prompt_version: str) -> list[dict]:
    """Assemble the versioned two-message diagnosis request."""
    system = "\n".join(
        (
            f"你是知识库自进化系统的失败诊断助手（诊断提示词版本：{prompt_version}）。",
            "你的唯一任务：为一个失败聚类生成结构化诊断草案。草案只是草案，不是已确认结论。",
            "",
            "【硬性规则】",
            "1. 只依据请求中给出的“证据包”进行判断；禁止引入证据包之外的常识、记忆或推测，证据不足时只能给出假设级结论。",
            "2. failure_category 必须取设计第 6 节失败分类法中的枚举值之一："
            + "、".join(_FAILURE_CATEGORIES)
            + "；不得自造类别。",
            "3. suggested_layer 必须取以下枚举值之一："
            + "、".join(_SUGGESTED_LAYERS)
            + "；confidence 必须取以下值之一："
            + "、".join(_CONFIDENCES)
            + "。",
            "4. direct_evidence 与 evidence_refs 只能引用证据包中实际出现的块哈希或管理员标签；禁止编造哈希、标签或原文。",
            "5. stage 必须与证据包中的 failure_stage 一致；reproducible 必须与证据包中的复现结论一致。",
            "6. 分类判定必须依据证据，按以下口径区分最相近的三个类别："
            "（a）recall_miss＝重检索结果里根本没有可能回答问题的块（注意块预览，判断依据是召回内容本身）；"
            "（b）ranking_error＝可能回答问题的块确实在召回候选里（failed_hashes 与 recall_hashes 有交集且块预览显示内容相关），"
            "但未被采用或排序靠后；"
            "（c）undetermined＝块预览显示召回内容确实不包含答案所需信息（如内容本身缺失、被脱敏、只有相邻主题），"
            "即失败源于知识库内容缺口而不是检索或生成缺陷。三者都指向“信息没到达生成器”时，选证据最直接支持的那个。",
            "7. 只输出单个 JSON 对象：不要 Markdown 代码块、注释、解释或任何多余文字。",
            "8. 输出中不得出现密钥、令牌、提示词指令本身或证据包之外的任何内容。",
            "",
            "【输出字段】JSON 对象必须包含以下 12 个字段，缺一不可：",
            *_FIELD_SPEC,
            "",
            "【本次证据包的判定规则】",
            *_conditional_rules(pack),
        )
    )
    user = "\n\n".join(
        (
            "# 聚类\n" + _render_cluster(cluster),
            "# 证据包\n" + _render_evidence_pack(pack),
            "请严格按系统提示输出单个 JSON 对象。",
        )
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _validate_completion(text: str) -> DiagnosisDraft:
    """One JSON object, all fields present, contract-valid values."""
    try:
        payload = generator._extract_json_object(text)
    except ValueError:
        raise DiagnosisUnavailable(REASON_INVALID, "response is not a JSON object") from None
    except generator.StructuredOutputError:
        raise DiagnosisUnavailable(REASON_INVALID, "structured_output_invalid") from None
    if not isinstance(payload, dict):
        raise DiagnosisUnavailable(REASON_INVALID, "response is not a JSON object")
    missing = [name for name in _DRAFT_FIELDS if name not in payload]
    if missing:
        raise DiagnosisUnavailable(REASON_INVALID, f"missing fields: {', '.join(missing)}")
    try:
        return DiagnosisDraft.model_validate(payload)
    except ValidationError as exc:
        locations = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
        raise DiagnosisUnavailable(REASON_INVALID, f"invalid fields: {locations}") from None


def _enforce_evidence_rules(draft: DiagnosisDraft, pack: EvidencePack) -> None:
    """Reject a model answer that contradicts the deterministic evidence.

    These are the hard post-validation rules: the draft is refused rather than
    repaired, so a cluster either gets a compliant draft or no draft at all.
    """
    problems: list[str] = []
    if not pack.admin_labels:
        if draft.confidence != "low":
            problems.append("no admin labels requires confidence=low")
        if "假设" not in draft.root_cause:
            problems.append("no admin labels requires a hypothesis root cause")
    if not pack.reproduction.reproducible:
        if draft.confidence != "low":
            problems.append("unreproducible requires confidence=low")
        if "待复核" not in draft.root_cause:
            problems.append("unreproducible requires a pending-review root cause")
        if draft.reproducible:
            problems.append("unreproducible requires reproducible=false")
    elif not draft.reproducible:
        problems.append("reproducible pack requires reproducible=true")
    if problems:
        raise DiagnosisUnavailable(REASON_INVALID, "; ".join(problems))


class Diagnoser:
    """One versioned structured diagnosis call per candidate cluster."""

    def __init__(
        self,
        model_call: ModelCall | None = None,
        *,
        prompt_version: str = DIAGNOSIS_PROMPT_VERSION,
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._model_call: ModelCall = model_call or _default_model_call(
            timeout_seconds if timeout_seconds is not None else 30.0,
            max_tokens if max_tokens is not None else 8000,
        )
        #: Prompt version of this instance; persisted next to every draft.
        self.prompt_version = prompt_version
        #: Model label for the draft row (Task 1 store ``model`` column).
        self.model_name = settings.deepseek_model
        #: Cost of the last call: prompt version, model, elapsed ms, output size.
        self.last_meta: dict[str, object] | None = None

    def draft(self, cluster: dict, pack: EvidencePack) -> DiagnosisDraft:
        """Diagnose one cluster with exactly one model call.

        Raises :class:`DiagnosisUnavailable` on any model, transport or
        contract failure; no partial draft is ever returned.
        """
        messages = _build_messages(cluster, pack, self.prompt_version)
        started = time.perf_counter()
        completion_chars = 0
        try:
            completion = self._model_call(messages)
            completion_chars = len(completion or "")
            draft = _validate_completion(completion)
            _enforce_evidence_rules(draft, pack)
        except DiagnosisUnavailable:
            self._record(started, completion_chars)
            raise
        except Exception as exc:
            reason, detail = _classify(exc)
            self._record(started, completion_chars)
            raise DiagnosisUnavailable(reason, detail) from exc
        self._record(started, completion_chars)
        return draft

    def _record(self, started: float, completion_chars: int) -> None:
        """Record call cost without storing prompt or completion text."""
        self.last_meta = {
            "prompt_version": self.prompt_version,
            "model": self.model_name,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "completion_chars": completion_chars,
        }


__all__ = ["DIAGNOSIS_PROMPT_VERSION", "Diagnoser", "DiagnosisUnavailable"]
