"""Constrained generation of code-patch candidates (phase 5, Task 2).

One confirmed diagnosis becomes at most one ``code_patch`` candidate through
exactly one model call. The input is already human-approved (the cluster
reached ``approved_for_candidate`` and its diagnosis draft was
administrator-confirmed), so the generator's job is narrow: turn the diagnosis
plus the **current** text of the files its layer may touch into one small,
bounded unified diff that a later gate layer can re-check deterministically.

The three hard pieces, in the order :meth:`PatchGenerator.generate` runs them:

1. **Target selection** (:func:`_select_target_files`) — the only files the
   model ever sees are real repository files inside the layer's allowed globs
   (``LAYER_PATH_GLOBS``), ranked by keywords from the confirmed diagnosis and
   truncated to 400 lines each. A diagnosis that names a concept instead of a
   module still lands on the module implementing it; a module named after a
   keyword always outranks a module that merely mentions it.
2. **The versioned prompt** (:data:`PATCH_PROMPT_VERSION`) — its contract (one
   unified diff, only allowed paths, the quotas, no deletion, no
   test/config/security/self-evolution/frontend edits, real ``test_paths``,
   the HEAD commit as the rollback base) is stated before the call and enforced
   again in the hard post-validation rules of :func:`_enforce_patch_rules`.
   A candidate is never repaired on failure: every breach raises
   :class:`PatchUnavailable`, so a cluster either yields a fully compliant
   candidate or nothing.
3. **Post-validation** — the deterministic layer-1 gate
   (:func:`~rag.evolution.code_patches.validate_patch`, including a real
   ``git apply --check``) runs on the assembled candidate, so an out-of-
   boundary path, a deletion, a quota breach or a diff that no longer applies
   is refused here rather than in a later gate.

The model channel mirrors the phase-3 diagnoser and phase-4 candidate generator
exactly (same ``settings.deepseek_*`` provider settings, isolated client,
bounded timeout and ``max_tokens``, thinking disabled, same failure
classification), because a background generation call must never trip the
online generator's circuit breaker and must stay inside the worker's call
budget. The small helpers that shape that channel (:func:`_default_model_call`
and :func:`_classify_failure`) are documented mirrors of their private
originals: ``rag/evolution/diagnoser.py`` and ``candidate_generator.py`` are
owned by other lanes and must not be edited here, while importing their private
names across modules would be worse than a mirror; the failure ``reason``
strings are already shared through the diagnoser's public constants.

No secrets leave this module: the request, the recorded call metadata
(:attr:`PatchGenerator.last_meta`) and the failure ``detail`` carry field
names, paths, counts and exception classes only — never diff content, model
output or credentials. The rollback base is read from git itself
(:func:`_git_head_commit`), never guessed and never taken from the model's
answer. Persistence is the caller's job: :meth:`generate` writes nothing.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import get_args

import openai
from pydantic import ValidationError

import rag.generator as generator
from config import settings
from rag.evolution.candidates import PayloadBoundaryError
from rag.evolution.code_patches import (
    ARTIFACT_TYPE,
    FORBIDDEN_GLOBS,
    LAYER_PATH_GLOBS,
    LAYERS,
    MAX_PATCH_CHANGED_LINES,
    MAX_PATCH_FILES,
    CodePatchCandidate,
    validate_patch,
)
from rag.evolution.diagnoser import REASON_INVALID, REASON_RATE_LIMITED, REASON_TIMEOUT
from rag.evolution.models import DiagnosisDraft
from rag.evolution.symbols import SymbolIndex, window_excerpt

#: Version of the patch prompt; stored next to every candidate so a prompt
#: revision can always be traced back (change the prompt, bump this).
PATCH_PROMPT_VERSION = "patch-v4"

type ModelCall = Callable[[list[dict]], str]

#: Patch temperature: low, close to deterministic, but not greedy — the same
#: discipline as the diagnosis and candidate prompts.
PATCH_TEMPERATURE = 0.2

#: How many allowed files may be shown to the model as generation context.
MAX_TARGET_FILES = 3

#: How many leading lines of one target file the prompt carries.
MAX_EXCERPT_LINES = 400

#: 补丁生成的完成 token 上限。这个 provider 的推理 token 计入该上限，而
#: ``thinking: disabled`` **并未真正生效**：2026-10-02 实测（temperature 0.2、同一提示词、
#: 30k 字符输入）每次调用花 **5076–10259** 个推理 token，正文在推理之后才输出。原先的
#: 16000 会在推理吃掉几乎全部预算时只剩 9 个字符、拿不到 diff（5 次尝试里 3 次如此）。
#: 因此默认预算按观测到的推理成本留足余量，而不是指望那个开关。
PATCH_MAX_TOKENS = 32000

#: How many files are read (bounded) before the content ranking picks the
#: final :data:`MAX_TARGET_FILES`. Reading every allowed file would be
#: unbounded on the open ``code`` layer, so a cheap path ranking shortlists
#: first and the content refines it.
MAX_PATH_SHORTLIST = 24

#: ``git rev-parse HEAD`` is bounded like the layer-1 ``git apply --check``.
GIT_HEAD_TIMEOUT_S = 30.0

#: A commit digest, the only shape ``rollback_ref["base_commit"]`` may take.
_BASE_COMMIT_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z")

#: Diagnosis keywords: ASCII identifiers (``reranker``, ``vector_store``) and
#: CJK bigrams both count — the diagnosis is written in Chinese while the
#: module names are English, so a single-language tokenizer would score every
#: file zero and the selection would degrade to a directory listing.
_ASCII_TOKEN_PATTERN = re.compile(r"[a-z][a-z0-9_]{2,}")
_CJK_RUN_PATTERN = re.compile(r"[\u4e00-\u9fff]+")

#: Line prefixes that make a line part of a unified diff. Everything else
#: (prose, the metadata trailer) ends the diff region. ``" "`` matters most:
#: an unchanged context line is the bulk of a real diff, and stopping at the
#: first one would truncate the patch to its headers.
_DIFF_LINE_PREFIXES = (
    " ",
    "+",
    "-",
    "@@ ",
    "diff ",
    "index ",
    "--- ",
    "+++ ",
    "new file",
    "deleted file",
    "rename ",
    "copy ",
    "similarity ",
    "old mode",
    "new mode",
    "Binary ",
    "GIT binary",
    "\\",
)

#: The metadata trailer the model appends after the diff.
_TRAILER_FIELDS = ("test_paths", "rationale", "expected_effect", "confidence")
_TRAILER_PATTERN = re.compile(r"^(test_paths|rationale|expected_effect|confidence)[：:]\s*(.*)$")
_TRAILER_VALUE_SPLIT = re.compile(r"[,，\s]+")

#: Draft fields whose text contributes file-selection keywords.
_KEYWORD_FIELDS = ("stage", "root_cause", "first_divergence", "acceptance_criteria", "suggested_layer")

#: Confidence values, read from the contract so they can never drift.
_CONFIDENCES: tuple[str, ...] = get_args(CodePatchCandidate.model_fields["confidence"].annotation)

_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, openai.APITimeoutError)
_RATE_LIMIT_ERRORS: tuple[type[BaseException], ...] = (openai.RateLimitError,)


class PatchUnavailable(RuntimeError):
    """One confirmed diagnosis could not be turned into a code-patch candidate.

    ``reason`` classifies the failure for the worker round: ``timeout`` and
    ``rate_limited`` are capacity signals worth another round, while ``invalid``
    means either the model answer broke the prompt contract (no diff, missing
    metadata, unparseable test paths) or the assembled candidate stepped
    outside the patch boundary — the same vocabulary, for the same reasons, as
    :class:`~rag.evolution.candidate_generator.CandidateUnavailable`.
    ``detail`` names failing fields, paths, counts and exception classes only:
    it never carries credentials, diff content or raw model output.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _classify_failure(exc: BaseException) -> tuple[str, str]:
    """Map a provider/transport failure onto a worker-level reason + detail.

    Documented mirror of ``rag.evolution.candidate_generator._classify_failure``
    (owned by another lane); the classification vocabulary is shared through the
    diagnoser's public reason constants. Any unrecognized failure is ``invalid``
    rather than a silent pass: an unknown provider error must not look like a
    capacity signal the worker should retry.
    """
    if isinstance(exc, PatchUnavailable):
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


def _default_model_call(timeout_seconds: float = 90.0, max_tokens: int = None) -> ModelCall:
    """Build an isolated model call for patch generation at low temperature.

    Same discipline as the diagnoser's and the candidate generator's default
    call, and for the same reasons: the call reuses the generator's provider
    settings and auth-header helper but owns its client, so a background
    generation timeout can never trip the online generator's circuit breaker;
    ``max_tokens`` bounds the completion because this provider's reasoning
    tokens count against the budget, and a diff plus 400-line file context is
    the largest completion this prompt can ask for.

    ``thinking: disabled`` does **not** actually stop the reasoning on this
    provider: 2026-10-02 probe measured 5076–10259 reasoning tokens per call at
    temperature 0.2 with the flag set, and the content is emitted only after
    them. A run whose reasoning fills the budget therefore returns a handful of
    characters and no diff (three of five attempts at ``max_tokens=16000`` came
    back with 9 characters). The default budget is set well above the observed
    reasoning cost instead of relying on the flag.
    """

    budget = PATCH_MAX_TOKENS if max_tokens is None else max_tokens

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
            temperature=PATCH_TEMPERATURE,
            max_tokens=budget,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return response.choices[0].message.content or "（模型未返回内容）"

    return call


# ------------------------------------------------------------------
# Path globs: which real files a layer may touch.
# ------------------------------------------------------------------


def _glob_patterns(glob: str) -> tuple[str, ...]:
    """The fnmatch patterns one plan glob means.

    Documented mirror of the Task-1 parser's conventions, kept here so the
    enumeration below reads the plan's table the way it was written:
    ``fnmatch`` has no ``**`` (its ``*`` already crosses ``/``), a trailing
    ``/**`` also admits the prefix itself (``rag/rerank*/**`` covers
    ``rag/reranker.py``), and a leading ``**/`` also admits the name at the
    repository root.
    """
    patterns = [glob]
    if glob.endswith("/**"):
        patterns.append(glob[: -len("/**")])
    if glob.startswith("**/"):
        patterns.append(glob[len("**/") :])
    return tuple(patterns)


def _matched_glob(path: str, globs: tuple[str, ...]) -> str | None:
    """The first glob in ``globs`` that matches ``path``, if any."""
    for glob in globs:
        if any(fnmatch.fnmatch(path, pattern) for pattern in _glob_patterns(glob)):
            return glob
    return None


def _forbidden_glob(path: str) -> str | None:
    """The blacklist glob ``path`` hits, if any (documented Task-1 mirror)."""
    return _matched_glob(path, FORBIDDEN_GLOBS)


def _repo_relative(path: Path, root: Path) -> str:
    """``path`` as a repository-relative POSIX string."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _layer_files(repo_root: Path, layer: str) -> list[str]:
    """Real repository files inside ``layer``'s allowed globs, in table order.

    The enumeration walks ``rag/**/*.py`` because every layer glob lives under
    ``rag/``, applies the blacklist first (an allowed layer can never reopen a
    forbidden file — the self-evolution package especially), and returns the
    files ordered by the plan's glob table, then shallower paths, then
    lexicographically. That static order is the selection's tie-break, so the
    plan's own reranking case (``rag/reranker.py``) needs no keyword hint.
    """
    globs = LAYER_PATH_GLOBS[layer]
    found: dict[str, int] = {}
    for candidate in repo_root.glob("rag/**/*.py"):
        if not candidate.is_file():
            continue
        posix = _repo_relative(candidate, repo_root)
        if _forbidden_glob(posix) is not None:
            continue
        for index, glob in enumerate(globs):
            if _matched_glob(posix, (glob,)) is not None:
                found.setdefault(posix, index)
                break
    return sorted(found, key=lambda path: (found[path], path.count("/"), path))


# ------------------------------------------------------------------
# Relevance ranking: which of the allowed files the diagnosis is about.
# ------------------------------------------------------------------


def _diagnosis_keywords(drafts: tuple[dict, ...], cluster: Mapping) -> tuple[str, ...]:
    """Keywords the confirmed diagnosis contributes to file relevance."""
    parts = [str(cluster.get("failure_stage") or "")]
    for fields in drafts:
        parts.extend(str(fields.get(name) or "") for name in _KEYWORD_FIELDS)
    text = "\n".join(parts).lower()
    keywords = set(_ASCII_TOKEN_PATTERN.findall(text))
    for run in _CJK_RUN_PATTERN.findall(text):
        keywords.update(run[index : index + 2] for index in range(len(run) - 1))
    return tuple(sorted(keywords))


def _path_hits(path: str, keywords: tuple[str, ...]) -> int:
    """How many keywords the file's own name carries."""
    stem = path.rsplit("/", 1)[-1].lower()
    return sum(1 for keyword in keywords if keyword in stem)


def _content_hits(excerpt: str, keywords: tuple[str, ...]) -> int:
    """How many keywords the file's text carries."""
    lowered = excerpt.lower()
    return sum(1 for keyword in keywords if keyword in lowered)


def _excerpt(path: Path) -> tuple[str, int, bool]:
    """``(text, total_lines, truncated)`` for one target file.

    The full text is read and only the first :data:`MAX_EXCERPT_LINES` lines
    are kept, so one huge module cannot flood the prompt; the truncation is
    reported back to the caller so the prompt can say so (a model asked to
    patch lines it cannot see would invent context).
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    truncated = len(lines) > MAX_EXCERPT_LINES
    excerpt = "\n".join(lines[:MAX_EXCERPT_LINES])
    return excerpt, len(lines), truncated


def _observation_exception_path(trace_store, observation_id: int) -> str:
    """The repository file the observation's exception frame names, or ``""``.

    The frame is ``rag/pipeline.py:700:_finalize_rag_context``; only the file matters here. A
    store that cannot answer, a missing observation or an unrecorded frame all yield ``""`` so
    the caller falls back to keyword ranking — never an error, never a guess.
    """
    if trace_store is None or observation_id is None:
        return ""
    try:
        observation = trace_store.get_observation(int(observation_id))
    except Exception:  # noqa: BLE001 - a store that cannot answer is not a failed generation
        return ""
    frame = str((observation or {}).get("details_exception_frame") or "").strip()
    if not frame:
        return ""
    return frame.split(":", 1)[0].strip()


def _observation_exception_line(trace_store, observation_id: int) -> int | None:
    """The line the observation's exception frame names, or ``None``.

    The stored frame is ``rag/pipeline.py:745:_finalize_rag_context``; the file alone tells the
    generator *which* module to show, and the line tells it *where inside* to look — without it a
    target whose failure is at line 745 is excerpted as the file's first 400 lines, which is
    exactly nothing.
    """
    if trace_store is None or observation_id is None:
        return None
    try:
        observation = trace_store.get_observation(int(observation_id))
    except Exception:  # noqa: BLE001 - a store that cannot answer is not a failed generation
        return None
    frame = str((observation or {}).get("details_exception_frame") or "").strip()
    parts = frame.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _select_target_files(
    repo_root: Path,
    files: list[str],
    keywords: tuple[str, ...],
    prefer: str = "",
    *,
    focused: tuple[tuple[str, int], ...] = (),
) -> tuple[tuple[str, str, int, bool, str], ...]:
    """``(path, excerpt, total_lines, truncated)`` for the 1–3 relevant files.

    Two ranked stages keep this bounded on the open ``code`` layer (109 files
    at the time of writing): a cheap path ranking shortlists
    :data:`MAX_PATH_SHORTLIST` files, then each shortlisted file is read once
    and a content ranking picks the finalists. The ranking key is
    ``(names-a-keyword, path hits, content hits)`` compared together, so a
    module named after the diagnosis always outranks a module that merely
    mentions the keyword — ``rag/vector_store.py`` for a diagnosis that says
    ``vector_store``, not whichever module happens to discuss vectors at
    length. Full ties fall back to the layer's static file order.

    ``prefer`` is the file an exception's last stack frame names, and it wins outright: it goes
    first and it is included even when the keyword ranking would leave it out. A frame is a
    fact about where the failure was raised, so ranking files by diagnosis wording can only
    lose to it — that is how a NotImplementedError-style crash in the evidence path ended up
    producing a patch to the router instead.
    """
    focus_line: dict[str, int] = {}
    for path, line in focused:
        clean = path.removeprefix("./")
        if clean in files and clean not in focus_line and line > 0:
            focus_line[clean] = int(line)
    ordered = list(files)
    wanted = prefer.removeprefix("./")
    if wanted and wanted in ordered:
        ordered.remove(wanted)
        ordered.insert(0, wanted)
    # 聚焦文件（帧、诊断点名的符号）排在关键词排序之前：它们是"事实"，关键词只是"线索"。
    for path in focus_line:
        if path in ordered:
            ordered.remove(path)
    focused_paths = [path for path in focus_line if path in files]
    ordered = focused_paths + ordered
    ranked = sorted(
        ordered,
        key=lambda path: (
            -int(path == wanted),
            -int(path in focus_line),
            -int(_path_hits(path, keywords) > 0),
            -_path_hits(path, keywords),
        ),
    )
    scored: list[tuple[str, str, int, bool, int, str]] = []
    for path in ranked[:MAX_PATH_SHORTLIST]:
        note = ""
        if path in focus_line:
            text = (repo_root / path).read_text(encoding="utf-8", errors="replace")
            excerpt, first, last, truncated = window_excerpt(text, center=focus_line[path])
            total_lines = len(text.splitlines())
            if truncated:
                note = f"仅显示第 {first}–{last} 行（共 {total_lines} 行），其余已省略"
        else:
            excerpt, total_lines, truncated = _excerpt(repo_root / path)
            if truncated:
                note = f"已截断：仅显示前 {MAX_EXCERPT_LINES} 行"
        scored.append(
            (path, excerpt, total_lines, truncated, _content_hits(excerpt, keywords), note)
        )
    # 聚焦文件排前（帧最前）；其余按"路径命中关键词 → 路径命中数 → 内容命中数 → 层内静态顺序"。
    scored.sort(
        key=lambda item: (
            item[0] != wanted,
            item[0] not in focus_line,
            -int(_path_hits(item[0], keywords) > 0),
            -_path_hits(item[0], keywords),
            -item[4],
            ordered.index(item[0]),
        )
    )
    return tuple(
        (path, excerpt, total_lines, truncated, note)
        for path, excerpt, total_lines, truncated, _, note in scored[:MAX_TARGET_FILES]
    )


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
    """Render the confirmed diagnosis drafts the patch must serve."""
    blocks = []
    for index, fields in enumerate(drafts, start=1):
        lines = [f"[诊断草案 {index}/{len(drafts)}]"]
        lines.extend(f"{name}: {_compact(fields.get(name))}" for name in DiagnosisDraft.model_fields)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _render_locate_note(
    exception_path: str, exception_line: int | None, symbols: tuple[object, ...]
) -> str:
    """The fact-based location block: the exception frame and any real symbol the diagnosis named.

    Rendered ahead of the file bodies so the model reads *where* before it reads *what*: the
    frame is where the failure was raised, and a named symbol is what the diagnosis is talking
    about. Both come from parsing, never from matching words against file names.
    """
    lines: list[str] = []
    if exception_path:
        where = f"{exception_path}:{exception_line}" if exception_line else exception_path
        lines.append(f"- 异常栈最后一帧：{where}")
    for symbol in symbols:
        lines.append(
            f"- 诊断点名的符号：{symbol.qualname}（{symbol.path}:{symbol.start}-{symbol.end}）"
        )
    if not lines:
        return ""
    return "# 定位（系统按符号表确定，不是关键词猜测）\n" + "\n".join(lines)


def _available_tests(repo_root: Path, paths: tuple[str, ...], *, limit: int = 12) -> tuple[str, ...]:
    """Existing test files that actually relate to the files this patch touches.

    Why this exists (measured): for cluster 30 the model named ``tests/test_jev_service.py`` in two
    of two attempts and the boundary refused both, because the file does not exist. The prompt showed
    the source files but never said which tests exist, so the only available strategy was guessing.
    This lists real names — never content — and it prefers tests that *import* the module being
    patched, falling back to name matching, so the list is small and relevant rather than a dump of
    the whole directory.
    """
    root = Path(repo_root)
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return ()
    modules = {
        path[: -len(".py")].replace("/", ".")
        for path in paths
        if path.endswith(".py")
    }
    stems = {Path(path).stem.lower() for path in paths}
    importers: list[str] = []
    by_name: list[str] = []
    for candidate in sorted(tests_dir.glob("test_*.py")):
        relative = f"tests/{candidate.name}"
        if any(stem and stem in candidate.name.lower() for stem in stems):
            by_name.append(relative)
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(f"from {module}" in text or f"import {module}" in text for module in modules):
            importers.append(relative)
    ordered = list(dict.fromkeys(importers + by_name))
    return tuple(ordered[:limit])


#: 一次重试时给模型的说明：**只带我们自己枚举出来的失败类别**，绝不带 diff、git 原文或任何
#: 用户/语料内容（git 的 stderr 会包含文件正文，所以它永远不进这条路径）。
_REPAIR_NOTES: dict[str, str] = {
    "corrupt_patch": (
        "上一版的 diff 结构不合法（hunk 头声明的行数与正文不符，或 hunk 不完整）。"
        "请重发一个完整自洽的 unified diff，并确保每个 @@ 头声明的行数与正文行数一致。"
    ),
    "does_not_apply": (
        "上一版的上下文行与仓库当前内容对不上（补丁套用失败）。"
        "请严格照上面给出的文件正文重写上下文行，不要凭记忆填写。"
    ),
    "unrecognized_input": (
        "上一版不是 git 能识别的 unified diff。只输出 diff 本身：不要围栏、解释或多余文字。"
    ),
    "missing_target": "上一版点到了仓库里不存在的文件。只允许修改上面列出的目标文件。",
    "already_exists": "上一版试图新建一个已存在的文件。请改为修改现有文件。",
}


#: 只有"diff 本身写坏了"这一类才值得重试：重发一次可能修好。策略/范围类拒绝（越界路径、
#: 禁止文件、删除文件、测试文件不存在、配额超限）重试也一定失败，白花一次调用。
_RETRYABLE_CATEGORIES: frozenset[str] = frozenset(
    {
        "corrupt_patch",
        "does_not_apply",
        "unrecognized_input",
        "no_valid_patch",
        # "压根没给出 diff"与"回复无法解析"都是格式失败（提示词里写明了要什么），重发一次值得。
        "diff_names_no_file",
        "unparseable_reply",
    }
)

#: 识别我们**自己**写进 reason/detail 的类别（不读 git 的原始 stderr——它带文件正文）。
#: 左侧是机器类别名（边界映射出来的），右侧是我们自己的英文措辞：两处都要认，否则
#: "diff names no file" 这种自有措辞会识别不出来，重试就不会触发。
_CATEGORY_HINTS: tuple[tuple[str, str], ...] = (
    # 回复压根解析不出契约（pydantic / 结构化输出 / JSON）：也是格式失败，值得重发一次。
    ("StructuredOutputError", "unparseable_reply"),
    ("structured_output_invalid", "unparseable_reply"),
    ("ValidationError", "unparseable_reply"),
    ("JSONDecodeError", "unparseable_reply"),
    ("diff names no file", "diff_names_no_file"),
    ("response carries no unified diff", "diff_names_no_file"),
    ("corrupt_patch", "corrupt_patch"),
    ("corrupt patch", "corrupt_patch"),
    ("does_not_apply", "does_not_apply"),
    ("does not apply", "does_not_apply"),
    ("unrecognized_input", "unrecognized_input"),
    ("unrecognized input", "unrecognized_input"),
    ("no_valid_patch", "no_valid_patch"),
    ("no valid patches", "no_valid_patch"),
    ("missing_target", "missing_target"),
    ("no such file", "missing_target"),
    ("already_exists", "already_exists"),
    ("already exists", "already_exists"),
)


def _failure_category(reason: str, detail: str) -> str:
    """The failure category, read from our own reason/detail rather than from git's output."""
    text = f"{reason} {detail}"
    for needle, category in _CATEGORY_HINTS:
        if needle in text:
            return category
    return str(reason)


def _repair_note(reason: str, detail: str) -> str:
    """The one retry's instruction: our own category, never the rejected output."""
    hint = _REPAIR_NOTES.get(str(reason), "请重新生成一个满足全部约束的 unified diff。")
    return (
        "# 上一次尝试被拒绝\n"
        f"失败类别：{reason}（{str(detail)[:80]}）\n"
        f"{hint}\n"
        "请重新输出：一个完整的 unified diff + 四行元数据。"
    )


def _render_available_tests(tests: tuple[str, ...]) -> str:
    """The verification-target block: real file names, or an explicit "none matched"."""
    if not tests:
        return (
            "# 可用的验证测试（仓库里实际存在的文件名）\n"
            "（没有匹配到你改动的测试文件：请**自己确认**后点名一个确实存在的 tests/ 文件，"
            "绝不编造文件名——沙箱会逐个核对存在性。）"
        )
    listed = "\n".join(f"- {name}" for name in tests)
    return (
        "# 可用的验证测试（仓库里实际存在的文件名，**只能从中选择**）\n"
        f"{listed}\n"
        "元数据的 test_paths 必须来自上面这个列表：编造一个不存在的文件名会被边界直接拒绝。"
    )


def _render_targets(targets: tuple[tuple[str, str, int, bool, str], ...]) -> str:
    """Render the selected target files, stating per file what is shown and what is not."""
    blocks = []
    for path, excerpt, total_lines, _truncated, note in targets:
        header = f"### 文件 {path}（共 {total_lines} 行）"
        if note:
            header += f"【{note}】"
        blocks.append(header + "\n" + excerpt)
    return "\n\n".join(blocks)


def _build_messages(
    cluster: Mapping,
    drafts: tuple[dict, ...],
    layer: str,
    targets: tuple[tuple[str, str, int, bool], ...],
    base_commit: str,
    prompt_version: str,
    locate_note: str = "",
    repo_root: str | Path = ".",
) -> list[dict]:
    """Assemble the versioned two-message patch request.

    The hard contract is stated before the call so the model knows the boundary
    it must stay inside, and every rule is repeated after it — in
    :func:`_enforce_patch_rules` — so a breach cannot become a candidate.
    """
    allowed = "、".join(LAYER_PATH_GLOBS[layer])
    forbidden = "、".join(FORBIDDEN_GLOBS)
    system = "\n".join(
        (
            f"你是知识库自进化系统的代码补丁生成助手（诊断提示词/候选提示词版本：{prompt_version}）。",
            "你的唯一任务：为一条已由管理员确认根因的失败诊断，生成一个代码补丁候选"
            "——对有界范围的小幅 unified diff。",
            "补丁只是候选：它还要经过沙箱验证与管理员审核才可能被人工应用；"
            "系统永不自动合入，你只负责给出小步、可验证、可回滚的 diff。",
            "",
            "【硬性规则】",
            "1. 只输出一个 unified diff：不要 Markdown 围栏、注释、解释或任何多余文字；"
            "diff 之后必须附加四行元数据（见规则 5）。",
            f"2. 只能修改本层允许位置内的文件（本次 layer={layer}，允许 {allowed}）；"
            f"禁止位置一律不得新增、修改或删除（{forbidden}）。",
            f"3. 补丁最多涉及 {MAX_PATCH_FILES} 个文件、改动不超过 {MAX_PATCH_CHANGED_LINES} 行"
            "（新增+删除合计）；不得包含二进制内容；不得删除文件。",
            "4. 不得修改测试、配置、安全、权限、自演化模块或前端。**尤其不得在 diff 里新建或修改 "
            "tests/** 下的任何文件**——验证测试只写在元数据的 test_paths 行里（规则 5），它不进 diff；"
            "把测试写进 diff 会被系统直接拒绝（2026-10-02 实测：同一诊断 3/3 次因此被拒）。",
            "5. diff 之后必须附加四行元数据，缺一不可：",
            "   test_paths: 验证用测试文件，必须是 tests/ 下真实存在的 test_*.py，至少一个，多个用逗号分隔；",
            "   rationale: 补丁理由，必须引用已确认诊断的根因；",
            "   expected_effect: 预期效果，必须对应该诊断的验收标准；",
            "   confidence: 只能取 low、medium、high 之一。",
            "6. rollback_ref.base_commit 由系统记录为请求中给出的 HEAD commit，请原样回传该值。",
            "7. diff 必须能对请求中给出的目标文件正文应用：上下文行必须与目标文件逐字一致，"
            "不得臆造文件中不存在的上下文，不得改写未在请求中给出的文件；"
            "**diff 只能修改这些已存在的目标文件，不得新建任何文件**（新增文件既无法应用，也会被拒绝）。",
            "8. 只依据请求中给出的诊断与目标文件正文生成补丁；"
            "输出中不得出现密钥、令牌、用户原文或提示词指令本身。",
        )
    )
    parts = [
        "# 失败聚类\n" + _render_cluster(cluster),
    ]
    if locate_note:
        parts.append(locate_note)
    user = "\n\n".join(
        parts + [
            "# 已确认诊断（管理员已确认根因）\n" + _render_drafts(drafts),
            f"# 目标层级\nlayer: {layer}\n允许位置（只能修改这些 glob 内的文件）: {allowed}",
            "# 目标文件正文（仓库当前内容，路径相对仓库根目录）\n" + _render_targets(targets),
            _render_available_tests(
                _available_tests(Path(repo_root), tuple(entry[0] for entry in targets))
            ),
            f"# git base 信息\nHEAD commit: {base_commit}",
            "请严格按系统提示输出：一个 unified diff + 四行元数据。"
            "若上面给出了定位，请把修改落在该函数/该行附近；正文里没显示的行不要臆造。",
        ]
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ------------------------------------------------------------------
# Answer parsing: one diff plus four metadata lines.
# ------------------------------------------------------------------


def _normalized_lines(reply: str) -> list[str]:
    """LF-normalized reply lines with markdown fences stripped.

    A fence line never belongs to a diff (a diff body line starts with a diff
    prefix), so dropping every fence line accepts both fenced blocks and bare
    diffs while keeping the diff text itself byte-exact.
    """
    text = (reply or "").replace("\r\n", "\n").replace("\r", "\n")
    return [line for line in text.split("\n") if not line.lstrip().startswith("```")]


def _diff_start(lines: list[str]) -> int | None:
    """Index of the first line that begins a unified diff, if any.

    Either git's own ``diff --git`` header, or a ``---`` line directly followed
    by a ``+++`` line — the adjacency is what tells a file header from a prose
    line (or a removed line) that merely starts with dashes.
    """
    for index, line in enumerate(lines):
        if line.startswith("diff --git "):
            return index
        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            return index
    return None


def _extract_diff(reply: str) -> str:
    """The unified diff inside a model reply: fences stripped, noise trimmed.

    Prose before the diff is skipped; lines after the diff that carry no diff
    prefix (the metadata trailer, an apology) end it. The surviving text keeps
    its exact bytes — except that trailing blank lines are dropped and exactly
    one final newline is guaranteed, because ``git apply`` rejects a diff whose
    last line is unterminated ("corrupt patch") and tolerates nothing else
    about the ending.
    """
    lines = _normalized_lines(reply)
    start = _diff_start(lines)
    if start is None:
        raise PatchUnavailable(REASON_INVALID, "response carries no unified diff")
    body: list[str] = []
    for line in lines[start:]:
        if not line or line.startswith(_DIFF_LINE_PREFIXES):
            body.append(line)
        else:
            break
    diff = "\n".join(body).rstrip("\n")
    if not diff.strip():
        raise PatchUnavailable(REASON_INVALID, "response carries an empty diff")
    return diff + "\n"


def _has_file_headers(diff: str) -> bool:
    """Whether the diff parses to at least one ``---``/``+++`` file header."""
    lines = diff.split("\n")
    return any(
        lines[index].startswith("--- ")
        and index + 1 < len(lines)
        and lines[index + 1].startswith("+++ ")
        for index in range(len(lines))
    )


def _diff_paths(diff: str) -> tuple[str, ...]:
    """Every file the diff touches, in diff order; ``/dev/null`` is not a file.

    Compact mirror of the Task-1 parser's header pairing (``---``/``+++``
    adjacency is what tells a file header from a removed line that merely starts
    with dashes) plus the rename/copy endpoints, which carry no headers at all.
    Both sides of a header can name a file (a rename with edits), so both are
    collected; a path the blacklist refuses is still collected here — the
    boundary check is what refuses it, and its reason names the path.
    """
    lines = diff.split("\n")
    paths: list[str] = []
    for index in range(len(lines) - 1):
        if lines[index].startswith("--- ") and lines[index + 1].startswith("+++ "):
            for header in (lines[index], lines[index + 1]):
                target = header[4:].split("\t", 1)[0].strip().strip('"')
                if target == "/dev/null":
                    continue
                paths.append(target[2:] if target.startswith(("a/", "b/")) else target)
    for line in lines:
        for prefix in ("rename from ", "rename to ", "copy from ", "copy to "):
            if line.startswith(prefix):
                paths.append(line[len(prefix) :].strip().strip('"'))
    ordered: list[str] = []
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    return tuple(ordered)


def _changed_line_count(diff: str) -> int:
    """Added plus deleted lines; file headers and hunk headers are not changes."""
    lines = diff.split("\n")
    headers = set()
    for index in range(len(lines) - 1):
        if lines[index].startswith("--- ") and lines[index + 1].startswith("+++ "):
            headers.update((index, index + 1))
    return sum(
        1
        for index, line in enumerate(lines)
        if index not in headers and line.startswith(("+", "-"))
    )


def _trailer_fields(reply: str) -> dict[str, str]:
    """The labelled metadata lines anywhere in the reply; first wins.

    Anchored at line start, so a diff line that happens to contain the text
    (a removed ``-test_paths: x`` line, a context line) never matches.
    """
    found: dict[str, str] = {}
    for line in _normalized_lines(reply):
        match = _TRAILER_PATTERN.match(line)
        if match is not None and match.group(1) not in found:
            found[match.group(1)] = match.group(2).strip()
    return found


def _parse_answer(reply: str) -> tuple[str, dict]:
    """``(diff, answer fields)`` from one model reply, or refuse it whole."""
    diff = _extract_diff(reply)
    fields = _trailer_fields(reply)
    missing = [name for name in _TRAILER_FIELDS if not fields.get(name)]
    if missing:
        raise PatchUnavailable(REASON_INVALID, f"missing fields: {', '.join(missing)}")
    test_paths = tuple(path for path in _TRAILER_VALUE_SPLIT.split(fields["test_paths"]) if path)
    if not test_paths:
        raise PatchUnavailable(REASON_INVALID, "test_paths is empty")
    confidence = fields["confidence"]
    if confidence not in _CONFIDENCES:
        raise PatchUnavailable(REASON_INVALID, "confidence must be one of low/medium/high")
    answer = {
        "diff": diff,
        "test_paths": test_paths,
        "rationale": fields["rationale"],
        "expected_effect": fields["expected_effect"],
        "confidence": confidence,
    }
    return diff, answer


# ------------------------------------------------------------------
# The git base.
# ------------------------------------------------------------------


def _git_head_commit(repo_root: Path) -> str:
    """The repository's HEAD commit, the base every generated diff applies to.

    ``git rev-parse HEAD`` through a bounded subprocess: the repository is the
    only authority for the commit a diff was generated against, and guessing it
    — or echoing a model-provided sha — would make ``rollback_ref`` a checkout
    to the wrong tree. Only the 40-hex digest is returned; git's stdout/stderr
    never leave this function.
    """
    try:
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GIT_HEAD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise PatchUnavailable(REASON_INVALID, "git head read timed out") from None
    except OSError:
        raise PatchUnavailable(REASON_INVALID, "git head unavailable") from None
    if completed.returncode != 0:
        raise PatchUnavailable(REASON_INVALID, "git head read failed")
    sha = (completed.stdout or "").strip()
    if not _BASE_COMMIT_PATTERN.match(sha):
        raise PatchUnavailable(REASON_INVALID, "git head is not a 40-hex commit")
    return sha


# ------------------------------------------------------------------
# Input and scope validation.
# ------------------------------------------------------------------


def _normalize_drafts(drafts: object) -> tuple[dict, ...]:
    """Accept ``DiagnosisDraft`` objects, stored draft rows or plain mappings.

    Documented mirror of the phase-4 normalizer (owned by another lane): the
    worker hands over whatever shape it read, so all three normalize to the
    same field mapping; anything missing a draft field is a contract breach,
    not something to repair.
    """
    if isinstance(drafts, Mapping) or not isinstance(drafts, (list, tuple)):
        raise PatchUnavailable(REASON_INVALID, "drafts must be a list of confirmed diagnosis drafts")
    if not drafts:
        raise PatchUnavailable(REASON_INVALID, "no confirmed diagnosis draft for this observation")
    normalized: list[dict] = []
    for index, item in enumerate(drafts):
        if isinstance(item, DiagnosisDraft):
            fields = item.model_dump(mode="json")
        elif isinstance(item, Mapping):
            nested = item.get("payload")
            fields = dict(nested) if isinstance(nested, Mapping) else dict(item)
        else:
            raise PatchUnavailable(REASON_INVALID, f"draft[{index}] is not a diagnosis draft")
        missing = [name for name in DiagnosisDraft.model_fields if name not in fields]
        if missing:
            raise PatchUnavailable(
                REASON_INVALID, f"draft[{index}] is missing fields: {', '.join(missing)}"
            )
        normalized.append(fields)
    return tuple(normalized)


def _validate_inputs(cluster: object, layer: object, cluster_id: object, repo_root: object) -> Path:
    """Reject an unusable request before spending a model call on it.

    Only the pieces the prompt and the target selection are built from are
    checked here; the hard patch rules stay in :func:`_enforce_patch_rules`.
    """
    if layer not in LAYERS:
        raise PatchUnavailable(REASON_INVALID, f"unknown patch layer: {layer!r}")
    if not isinstance(cluster, Mapping):
        raise PatchUnavailable(REASON_INVALID, "cluster must be a mapping")
    cluster_row_id = cluster.get("id")
    if cluster_row_id is not None and int(cluster_row_id) != int(cluster_id):
        raise PatchUnavailable(REASON_INVALID, "cluster_id disagrees with the cluster row")
    root = Path(repo_root)
    if not root.is_dir():
        raise PatchUnavailable(REASON_INVALID, "repo_root is not a directory")
    return root


def _enforce_diagnosis_scope(
    drafts: tuple[dict, ...],
    cluster: Mapping,
    observation_id: int,
    trace_store,
    layer: str,
) -> None:
    """The confirmed diagnosis must address this layer and belong to this cluster.

    Narrowed mirror of the phase-4 scope rule: a code patch can only fix the
    layer its diagnosis recommends (a diagnosis of another layer cannot justify
    editing this cluster's code), and the diagnosis must agree with the cluster
    and the member observation on stage and kb. ``trace_store`` is read — never
    written — like the phase-4 generator.

    One override exists by design (design section 6/8): when the administrator
    confirmed a root-cause layer on the cluster, that confirmation is the
    authority and supersedes the diagnoser's own suggestion — the requested layer
    must equal the confirmed one, and the drafts' suggestions are not consulted
    for the layer decision.
    """
    confirmed = str(cluster.get("confirmed_layer") or "").strip()
    if confirmed and confirmed != layer:
        raise PatchUnavailable(
            REASON_INVALID,
            f"the administrator confirmed the {confirmed!r} layer, so no {layer!r} patch "
            "may be generated for this cluster",
        )
    for index, fields in enumerate(drafts):
        if confirmed:
            # The human authority governs the layer; nothing else to check here.
            continue
        suggested = str(fields.get("suggested_layer") or "").strip()
        if suggested != layer:
            raise PatchUnavailable(
                REASON_INVALID,
                f"draft[{index}].suggested_layer {suggested!r} is not the {layer} layer "
                "this patch addresses",
            )
    cluster_stage = str(cluster.get("failure_stage") or "").strip()
    cluster_kb = str(cluster.get("kb_id") or "").strip()
    try:
        observation = trace_store.get_observation(observation_id)
    except Exception as exc:
        raise PatchUnavailable(
            REASON_INVALID, f"observation read failed: {type(exc).__name__}"
        ) from exc
    if observation is None:
        raise PatchUnavailable(REASON_INVALID, f"observation {observation_id} not found")
    observation_kb = str(observation.get("kb_id") or "").strip()
    if cluster_kb and observation_kb and observation_kb != cluster_kb:
        raise PatchUnavailable(REASON_INVALID, "diagnosis kb disagrees with the cluster kb")
    for index, fields in enumerate(drafts):
        stage = str(fields.get("stage") or "").strip()
        if cluster_stage and stage and stage.lower() != cluster_stage.lower():
            raise PatchUnavailable(
                REASON_INVALID,
                f"draft[{index}].stage {stage!r} disagrees with the cluster "
                f"failure_stage {cluster_stage!r}",
            )


# ------------------------------------------------------------------
# The hard post-validation rules.
# ------------------------------------------------------------------


def _enforce_patch_rules(candidate: CodePatchCandidate, *, layer: str, repo_root: Path, diff: str) -> None:
    """The hard rules every answer must obey; a breach refuses it whole.

    1. the layer is one the contract declares;
    2. ``test_paths`` is non-empty — existence is :func:`validate_patch`'s job;
    3. the diff is non-empty and parses to at least one ``---``/``+++`` file
       header;
    4. the candidate passes the deterministic layer-1 boundary gate
       (:func:`~rag.evolution.code_patches.validate_patch`: blacklist, layer
       whitelist, quotas, ``test_paths`` existence and a real ``git apply
       --check``) — whose reason names paths, globs, counts and git categories
       only, never diff content.
    """
    if layer not in LAYERS:
        raise PatchUnavailable(REASON_INVALID, f"unknown patch layer: {layer!r}")
    if not candidate.test_paths:
        raise PatchUnavailable(REASON_INVALID, "test_paths is empty")
    if not diff.strip():
        raise PatchUnavailable(REASON_INVALID, "diff is empty")
    if not _has_file_headers(diff):
        raise PatchUnavailable(REASON_INVALID, "diff has no unified file header")
    try:
        validate_patch(candidate, repo_root=repo_root)
    except PayloadBoundaryError as error:
        raise PatchUnavailable(REASON_INVALID, error.reason) from None


def _build_candidate(
    answer: Mapping, paths: tuple[str, ...], *, layer: str, base_commit: str
) -> CodePatchCandidate:
    """Assemble the frozen candidate; a contract breach refuses it whole."""
    try:
        return CodePatchCandidate(
            artifact_type=ARTIFACT_TYPE,
            layer=layer,
            paths=paths,
            diff=answer["diff"],
            test_paths=answer["test_paths"],
            rationale=answer["rationale"],
            expected_effect=answer["expected_effect"],
            rollback_ref={"base_commit": base_commit},
            confidence=answer["confidence"],
        )
    except ValidationError as exc:
        locations = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
        raise PatchUnavailable(REASON_INVALID, f"candidate contract: {locations}") from None


class PatchGenerator:
    """One versioned, bounded patch generation call per confirmed diagnosis."""

    def __init__(
        self,
        model_call: ModelCall | None = None,
        *,
        prompt_version: str = PATCH_PROMPT_VERSION,
        timeout_seconds: float = 90.0,
        max_tokens: int = PATCH_MAX_TOKENS,
    ) -> None:
        #: 本次生成实际发生的模型调用次数（含重试），供预算记账。
        self.model_calls = 0
        self._model_call: ModelCall = model_call or _default_model_call(
            timeout_seconds, max_tokens
        )
        #: Prompt version of this instance; persisted next to every candidate.
        self.prompt_version = prompt_version
        #: Model label for the candidate row (the store ``model`` column).
        self.model_name = settings.deepseek_model
        #: Cost of the last call: prompt version, model, elapsed ms, output size.
        self.last_meta: dict[str, object] | None = None

    def generate(
        self,
        cluster: dict,
        drafts: list,
        *,
        layer: str,
        repo_root: str | Path,
        observation_id: int,
        cluster_id: int,
        trace_store,
    ) -> CodePatchCandidate:
        """Generate one code-patch candidate for a confirmed diagnosis, or refuse.

        ``cluster`` is the approved cluster row and ``drafts`` the
        administrator-confirmed diagnosis drafts of ``observation_id``;
        ``layer`` is the evolution layer the patch addresses and ``repo_root``
        the repository it applies to. The target files are read (never
        written), ``trace_store`` is read for the diagnosis' consistency
        signals and the git HEAD is read for the rollback base. Returns a
        validated candidate or raises :class:`PatchUnavailable`; persistence is
        the caller's job (``CandidateStore.add_candidate``).
        """
        normalized = _normalize_drafts(drafts)
        root = _validate_inputs(cluster, layer, cluster_id, repo_root)
        files = _layer_files(root, layer)
        if not files:
            raise PatchUnavailable(
                REASON_INVALID, f"layer {layer} has no candidate files in the repository"
            )
        keywords = _diagnosis_keywords(normalized, cluster)
        # 事实优先于线索：异常帧给"哪个文件哪一行"，符号索引把诊断里**真实存在**的标识符
        # 解析成文件与行区间（中文诊断不再退化为字母序）。
        index = SymbolIndex.build(root, files)
        exception_path = _observation_exception_path(trace_store, observation_id)
        exception_line = _observation_exception_line(trace_store, observation_id)
        diagnosis_text = "\n".join(
            str(fields.get(name) or "") for fields in normalized for name in _KEYWORD_FIELDS
        )
        named_symbols = index.symbols_in_text(diagnosis_text)
        focused: list[tuple[str, int]] = []
        if exception_path and exception_line:
            focused.append((exception_path, exception_line))
        focused.extend((symbol.path, symbol.start) for symbol in named_symbols)
        targets = _select_target_files(
            root, files, keywords, prefer=exception_path, focused=tuple(focused)
        )
        locate_note = _render_locate_note(exception_path, exception_line, named_symbols)
        base_commit = _git_head_commit(root)
        messages = _build_messages(
            cluster, normalized, layer, targets, base_commit, self.prompt_version,
            locate_note=locate_note, repo_root=root,
        )

        started = time.perf_counter()
        completion_chars = 0
        target_count = len(targets)
        # 一次生成 + 至多一次"带失败类别"的重试。实测动机：约每五次生成就有一次被边界拒绝
        # （上下文对不上 / hunk 不合法 / 编造测试文件名），而重试只需要把我们自己的类别说清楚。
        self.model_calls = 0

        def attempt_once(note: str):
            nonlocal completion_chars
            messages_for_attempt = (
                messages if not note else [*messages, {"role": "user", "content": note}]
            )
            completion = self._model_call(messages_for_attempt)
            completion_chars = len(completion or "")
            diff, answer = _parse_answer(completion)
            paths = _diff_paths(diff)
            if not paths:
                raise PatchUnavailable(REASON_INVALID, "diff names no file")
            changed = _changed_line_count(diff)
            candidate = _build_candidate(answer, paths, layer=layer, base_commit=base_commit)
            _enforce_patch_rules(candidate, layer=layer, repo_root=root, diff=diff)
            _enforce_diagnosis_scope(normalized, cluster, observation_id, trace_store, layer)
            return candidate, len(paths), changed

        repair_note = ""
        for attempt in (1, 2):
            self.model_calls += 1
            try:
                candidate, patch_files, changed = attempt_once(repair_note)
            except Exception as exc:
                reason, detail = _classify_failure(exc)
                category = _failure_category(reason, detail)
                if attempt == 2 or category not in _RETRYABLE_CATEGORIES:
                    self._record(started, completion_chars, target_count, 0, 0)
                    if isinstance(exc, PatchUnavailable):
                        raise
                    raise PatchUnavailable(reason, detail) from exc
                repair_note = _repair_note(category, detail)
                continue
            self._record(started, completion_chars, target_count, patch_files, changed)
            return candidate
        raise PatchUnavailable(REASON_INVALID, "patch generation produced nothing")

    def _record(
        self,
        started: float,
        completion_chars: int,
        target_files: int,
        patch_files: int,
        changed_lines: int,
    ) -> None:
        """Record call cost without storing prompt, diff or completion text."""
        self.last_meta = {
            "prompt_version": self.prompt_version,
            "model": self.model_name,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "completion_chars": completion_chars,
            "target_files": target_files,
            "patch_files": patch_files,
            "changed_lines": changed_lines,
        }


__all__ = [
    "MAX_EXCERPT_LINES",
    "MAX_TARGET_FILES",
    "PATCH_MAX_TOKENS",
    "PATCH_PROMPT_VERSION",
    "PATCH_TEMPERATURE",
    "PatchGenerator",
    "PatchUnavailable",
]
