"""Task 2: the constrained code-patch generator.

Every test injects a fake ``model_call`` so nothing here performs a network
call, and the diffs are built against the real working tree (the same
LF-normalized fixtures ``tests/test_code_patches.py`` uses, because this
repository's git runs with ``core.autocrlf=true``) — the post-validation runs a
real ``git apply --check`` against the real repository. The behaviours under
test:

* a small, contract-complete unified diff over an allowed module yields a
  :class:`~rag.evolution.code_patches.CodePatchCandidate` whose
  ``rollback_ref`` records the repository HEAD the diff applies to (read from
  git, never guessed or echoed from the model);
* the hard post-validation rules refuse a candidate instead of repairing it —
  an out-of-boundary path (the evolution package itself, the test baseline, a
  file of another layer), a deletion, an empty/broken/non-applying diff, more
  than five files, missing or non-existent ``test_paths`` — always as
  ``PatchUnavailable("invalid")`` and never as a half-validated candidate;
* provider failures classify as ``timeout`` / ``rate_limited`` / ``invalid``
  exactly like the phase-3 diagnoser, with exactly one model call each;
* the versioned prompt carries the hard contract, the cluster identity, the
  confirmed diagnosis, the selected target files and the HEAD commit — and
  neither the request nor the recorded metadata carries credentials.

The model answer contract under test is the one the prompt states: one unified
diff (bare or fenced, noise tolerated around it) followed by four labelled
metadata lines (``test_paths`` / ``rationale`` / ``expected_effect`` /
``confidence``).
"""

from __future__ import annotations

import difflib
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, RateLimitError

from config import settings
from rag.evolution.models import DiagnosisDraft, FailureObservation
from rag.evolution.patch_generator import (
    PATCH_PROMPT_VERSION,
    PATCH_TEMPERATURE,
    PatchGenerator,
    PatchUnavailable,
)
from rag.evolution.store import FailureTraceStore

SECRET_SENTINEL = "sk-patch-sentinel-do-not-leak"
PROBE_COMMENT = "# code-patch generator probe (comment only)"
TARGET_FILE = "rag/reranker.py"
TARGET_TEST = "tests/test_reranker.py"
CHUNKER_MARKER = "from langchain_text_splitters import RecursiveCharacterTextSplitter"
RERANKER_MARKER = '"""Re-ranking module using SiliconFlow Rerank API."""'
VECTOR_STORE_MARKER = "def search_collection(collection_name: str, query_embedding: list[float], top_k"
TRUNCATED_AWAY_MARKER = "def delete_doc(collection_name: str, doc_name: str):"
REPO_ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------
# Diff fixtures: real, parseable diffs against the real working tree.
# ------------------------------------------------------------------


def edit_file_diff(path: str = TARGET_FILE) -> str:
    """A real diff: one comment line appended to an existing file."""
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    edited = [*lines, f"{PROBE_COMMENT}\n"]
    return "".join(
        difflib.unified_diff(lines, edited, fromfile=f"a/{path}", tofile=f"b/{path}")
    )


def new_file_diff(path: str) -> str:
    """A well-formed diff that creates ``path`` (legal inside an allowed glob)."""
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,2 @@\n"
        f"+{PROBE_COMMENT}\n"
        "+value = 1\n"
    )


def non_applying_diff(path: str) -> str:
    """A well-formed diff whose preimage does not match the working tree."""
    return (
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,3 +1,4 @@\n"
        ' """preimage line that is not in the file"""\n'
        ' """second preimage line that is not in the file"""\n'
        ' """third preimage line that is not in the file"""\n'
        "+# line the patch tries to add\n"
    )


def delete_file_diff(path: str) -> str:
    """A diff whose ``+++`` side is ``/dev/null``: it deletes the file."""
    lines = (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        f"-{lines[0]}\n"
        f"-{lines[1]}\n"
    )


def model_reply(
    diff: str,
    *,
    test_paths: str = TARGET_TEST,
    rationale: str = "reranker 打分未惩罚词面撞名，与已确认根因一致。",
    expected_effect: str = "Top-5 命中回升，路线不变。",
    confidence: str = "medium",
) -> str:
    """The full model answer: one unified diff plus the four metadata lines."""
    return "\n".join(
        (
            "好的，这是为本次诊断生成的补丁。",
            diff.rstrip("\n"),
            "",
            f"test_paths: {test_paths}",
            f"rationale: {rationale}",
            f"expected_effect: {expected_effect}",
            f"confidence: {confidence}",
        )
    )


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
    """A contract-complete diagnosis draft confirming one ranking failure."""
    fields = {
        "failure_category": "ranking_error",
        "stage": "retrieval",
        "first_divergence": "重排候选中词面撞名段落排在相关段落之前，Top-5 得分区分度低。",
        "direct_evidence": "管理员标签标注相关段落未被采纳。",
        "root_cause": "reranker 打分后未做撞名惩罚，相关段落分数被淹没。",
        "impact_scope": "kb-finance 的段落重排序。",
        "suggested_layer": "reranking",
        "reproduction_steps": "用原问题重放，读取 rerank 排序结果。",
        "acceptance_criteria": "Top-5 命中回升且路线不变。",
        "confidence": "medium",
        "reproducible": True,
        "evidence_refs": ("label-1",),
    }
    fields.update(overrides)
    return DiagnosisDraft(**fields)


def seeded(tmp_path, *, cluster_kb: str = "kb-finance", observation_kb: str = "kb-finance"):
    """``(trace_store, cluster, member)``: one cluster with one member observation."""
    trace = FailureTraceStore(tmp_path / "evolution.db")
    now = time.time()
    cluster = trace.upsert_cluster(
        cluster_key="patch-1",
        kb_id=cluster_kb,
        failure_stage="retrieval",
        root_cause_feature="ranking_error",
        seen_at=now,
    )
    member = trace.add_observation(
        FailureObservation(
            kb_id=observation_kb,
            failure_stage="retrieval",
            question="相关段落为何排在撞名段落之后",
            route="rag",
            chunk_hashes=("a" * 64,),
            best_score=0.31,
            answerability_kind="insufficient_evidence",
        )
    )
    trace.attach_cluster_member(
        member["id"],
        cluster["id"],
        failure_stage="retrieval",
        root_cause_feature="ranking_error",
        now=now,
    )
    return trace, cluster, member


def _generate(
    fake: FakeModel,
    cluster: dict,
    member: dict,
    trace,
    *,
    drafts=None,
    layer: str = "reranking",
    **kwargs,
):
    """``generate`` over the real repository with the interface's defaults."""
    defaults = dict(
        layer=layer,
        repo_root=REPO_ROOT,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )
    defaults.update(kwargs)
    return PatchGenerator(model_call=fake).generate(
        cluster, [_draft()] if drafts is None else drafts, **defaults
    )


def _request_text(fake: FakeModel) -> str:
    return "".join(str(message.get("content", "")) for request in fake.requests for message in request)


# ------------------------------------------------------------------
# The happy path: one small diff becomes a contract-complete candidate.
# ------------------------------------------------------------------


def test_small_comment_diff_yields_a_contract_complete_candidate(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    reply = model_reply(edit_file_diff(TARGET_FILE))
    fake = FakeModel(reply)
    generator = PatchGenerator(model_call=fake)

    candidate = generator.generate(
        cluster,
        [_draft()],
        layer="reranking",
        repo_root=REPO_ROOT,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )

    assert candidate.artifact_type == "code_patch"
    assert candidate.layer == "reranking"
    assert candidate.paths == (TARGET_FILE,)
    assert candidate.diff.startswith("--- a/")
    assert PROBE_COMMENT in candidate.diff
    assert candidate.test_paths == (TARGET_TEST,)
    assert candidate.rationale == "reranker 打分未惩罚词面撞名，与已确认根因一致。"
    assert candidate.expected_effect == "Top-5 命中回升，路线不变。"
    assert candidate.confidence == "medium"
    # Rollback names the commit the diff applies to, read from git itself.
    assert candidate.rollback_ref == {"base_commit": repo_head()}
    # Exactly one model call per generation; the cost is recorded.
    assert len(fake.requests) == 1
    assert generator.last_meta is not None
    assert generator.last_meta["prompt_version"] == PATCH_PROMPT_VERSION
    assert generator.last_meta["model"] == settings.deepseek_model
    assert generator.last_meta["completion_chars"] == len(reply)
    assert generator.last_meta["patch_files"] == 1
    assert generator.last_meta["changed_lines"] == 1
    assert generator.last_meta["target_files"] == 1


def test_prompt_version_is_traceable_on_the_instance_and_in_the_request(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(edit_file_diff()))
    generator = PatchGenerator(model_call=fake)

    generator.generate(
        cluster,
        [_draft()],
        layer="reranking",
        repo_root=REPO_ROOT,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )

    # patch-v2：把「测试只进元数据、不进 diff」与「不得新建文件」写死——2026-10-02 实测
    # 同一诊断 3/3 次因 diff 里新建 tests/ 文件被拒。
    # patch-v5：定位块 + 靶心窗口 + 实际存在的测试文件名，并新增 edits 输出格式
    #（模型给精确替换块，我们用 difflib 拼 diff ⇒ hunk 头构造上自洽）。
    assert PATCH_PROMPT_VERSION == "patch-v5"
    assert generator.prompt_version == PATCH_PROMPT_VERSION
    assert PATCH_PROMPT_VERSION in _request_text(fake)


def test_store_row_shaped_drafts_are_accepted(tmp_path):
    """The worker reads stored rows; a row's nested payload is the draft."""
    trace, cluster, member = seeded(tmp_path)
    row = {
        "id": 1,
        "cluster_id": cluster["id"],
        "observation_id": member["id"],
        "payload": _draft().model_dump(mode="json"),
        "prompt_version": "diagnosis-v2",
        "model": "deepseek/deepseek-v4.1-flash",
    }
    fake = FakeModel(model_reply(edit_file_diff()))

    candidate = PatchGenerator(model_call=fake).generate(
        cluster,
        [row],
        layer="reranking",
        repo_root=REPO_ROOT,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )

    assert candidate.paths == (TARGET_FILE,)


def test_generate_never_writes_to_the_store(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    _generate(FakeModel(model_reply(edit_file_diff())), cluster, member, trace)

    tables = {
        row["name"]
        for row in trace._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "candidates" not in tables
    assert "evolution_candidate_audit" not in tables


def test_a_fenced_reply_with_prose_around_the_diff_is_extracted(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    reply = (
        "好的，这是为本次诊断生成的补丁。\n"
        "```diff\n" + edit_file_diff().rstrip("\n") + "\n```\n"
        "以上是补丁说明，请查收。\n"
        f"test_paths: {TARGET_TEST}\n"
        "rationale: reranker 打分未惩罚词面撞名，与已确认根因一致。\n"
        "expected_effect: Top-5 命中回升，路线不变。\n"
        "confidence: medium\n"
    )
    fake = FakeModel(reply)

    candidate = _generate(fake, cluster, member, trace)

    assert candidate.paths == (TARGET_FILE,)
    assert "```" not in candidate.diff
    assert PROBE_COMMENT in candidate.diff
    assert candidate.diff.rstrip("\n").endswith(f"+{PROBE_COMMENT}")


# ------------------------------------------------------------------
# Input validation: refuse before spending a model call.
# ------------------------------------------------------------------


@pytest.mark.parametrize("layer", ["prompt", "query_rewrite", "retrieval_config", "none", ""])
def test_unknown_layer_is_refused_before_the_call(tmp_path, layer):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(edit_file_diff()))

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace, layer=layer)

    assert excinfo.value.reason == "invalid"
    assert excinfo.value.detail.startswith("unknown patch layer")
    assert len(fake.requests) == 0


def test_a_layer_without_candidate_files_is_refused(tmp_path):
    """An empty repository has nothing under the layer globs: no call is spent."""
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(edit_file_diff()))

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace, repo_root=tmp_path)

    assert excinfo.value.reason == "invalid"
    assert "candidate files" in excinfo.value.detail
    assert len(fake.requests) == 0


def test_empty_draft_list_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(FakeModel(model_reply(edit_file_diff())), cluster, member, trace, drafts=[])

    assert excinfo.value.reason == "invalid"


def test_draft_of_another_layer_is_refused(tmp_path):
    """A code patch can only fix the layer its confirmed diagnosis recommends."""
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(edit_file_diff()))

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace, drafts=[_draft(suggested_layer="chunking")])

    assert excinfo.value.reason == "invalid"
    assert "suggested_layer" in excinfo.value.detail


def test_cluster_id_that_disagrees_with_the_cluster_row_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(edit_file_diff()))

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace, cluster_id=cluster["id"] + 999)

    assert excinfo.value.reason == "invalid"
    assert "cluster_id" in excinfo.value.detail


# ------------------------------------------------------------------
# Post-validation: every breach is invalid, never a half-validated patch.
# ------------------------------------------------------------------


def test_a_diff_touching_a_blacklisted_path_is_refused(tmp_path):
    """Self-modification: the model may not touch ``rag/evolution/**``."""
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(
        model_reply(edit_file_diff("rag/evolution/code_patches.py"), test_paths=TARGET_TEST)
    )

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace, layer="code", drafts=[_draft(suggested_layer="code")])

    assert excinfo.value.reason == "invalid"
    assert "rag/evolution/code_patches.py" in excinfo.value.detail
    assert "rag/evolution/**" in excinfo.value.detail


def test_a_diff_touching_the_test_baseline_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(
        model_reply(
            new_file_diff("tests/test_generated_baseline.py"),
            test_paths="tests/test_reranker.py",
        )
    )

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace, layer="code", drafts=[_draft(suggested_layer="code")])

    assert excinfo.value.reason == "invalid"
    assert "tests/test_generated_baseline.py" in excinfo.value.detail


def test_a_diff_outside_the_layer_globs_is_refused(tmp_path):
    """A reranking patch may not rewrite the chunker."""
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(edit_file_diff("rag/chunker.py")))

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"
    assert "rag/chunker.py" in excinfo.value.detail


def test_a_diff_that_deletes_a_file_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(delete_file_diff(TARGET_FILE)))

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"
    assert "delet" in excinfo.value.detail.lower()


def test_a_diff_with_more_than_five_files_is_refused(tmp_path):
    paths = tuple(f"rag/fusion/module_{index}.py" for index in range(6))
    diff = "".join(new_file_diff(path) for path in paths)
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(diff))

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"
    assert "6" in excinfo.value.detail and "5" in excinfo.value.detail


def test_a_diff_that_does_not_apply_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    # 两段同样的回复：第一段触发一次"带类别的重试"，第二段证明重试后仍然被拒绝。
    fake = FakeModel(
        model_reply(non_applying_diff(TARGET_FILE)),
        model_reply(non_applying_diff(TARGET_FILE)),
    )

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert len(fake.requests) == 2  # 确实重试过一次
    assert excinfo.value.reason == "invalid"
    assert "git apply --check failed" in excinfo.value.detail
    assert "preimage line" not in excinfo.value.detail


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "抱歉，我无法处理这个请求。",
        "```diff\n随便的内容，没有文件头。\n```",
        "@@ -1,1 +1,1 @@\n+x",
        model_reply("", test_paths=TARGET_TEST),
    ],
)
def test_an_empty_or_unparseable_diff_is_refused(tmp_path, reply):
    trace, cluster, member = seeded(tmp_path)
    # 空/无法解析的回复属于**格式**失败 ⇒ 会带类别重试一次；两次都拿不到 diff 才拒绝。
    fake = FakeModel(reply, reply)

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"
    assert len(fake.requests) == 2


def test_a_reply_without_the_metadata_lines_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel("好的。\n" + edit_file_diff() + "\n以上就是补丁。")

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"
    assert "missing fields" in excinfo.value.detail


def test_missing_test_paths_are_refused(tmp_path):
    reply = "\n".join(
        (
            edit_file_diff().rstrip("\n"),
            "",
            "rationale: reranker 打分未惩罚词面撞名。",
            "expected_effect: Top-5 命中回升。",
            "confidence: medium",
        )
    )
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(reply)

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"
    assert "test_paths" in excinfo.value.detail


def test_nonexistent_test_paths_are_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(
        model_reply(edit_file_diff(), test_paths="tests/test_does_not_exist_xyz.py")
    )

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"
    assert "tests/test_does_not_exist_xyz.py" in excinfo.value.detail


def test_a_test_path_outside_tests_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(edit_file_diff(), test_paths="rag/reranker.py"))

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"
    assert "rag/reranker.py" in excinfo.value.detail


def test_an_out_of_enum_confidence_is_refused(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(edit_file_diff(), confidence="certain"))

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"
    assert "confidence" in excinfo.value.detail


# ------------------------------------------------------------------
# Failure classification, exactly like the phase-3 diagnoser.
# ------------------------------------------------------------------


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

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == reason
    assert len(fake.requests) == 1


# ------------------------------------------------------------------
# The prompt contract, target-file selection and the secrets boundary.
# ------------------------------------------------------------------


def test_prompt_carries_the_version_stamp_contract_targets_and_base_commit(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(edit_file_diff()))

    _generate(fake, cluster, member, trace)

    prompt = _request_text(fake)
    # Version stamp and cluster identity travel with the request.
    assert f"诊断提示词/候选提示词版本：{PATCH_PROMPT_VERSION}" in prompt
    assert "kb-finance" in prompt
    # The confirmed diagnosis is the only reasoning input.
    assert "已确认诊断" in prompt
    assert "reranker 打分后未做撞名惩罚，相关段落分数被淹没。" in prompt
    # The selected target file's current text is shown.
    assert RERANKER_MARKER in prompt
    assert CHUNKER_MARKER not in prompt
    # The git base the rollback records.
    assert repo_head() in prompt
    # The hard contract, stated for the model.
    assert "unified diff" in prompt
    assert "rag/rerank*/**" in prompt
    assert "rag/evolution/**" in prompt
    assert "5" in prompt and "300" in prompt
    assert "删除文件" in prompt
    assert "test_paths" in prompt
    assert "tests/" in prompt
    assert "low" in prompt and "medium" in prompt and "high" in prompt


def test_target_files_come_from_the_layer_globs(tmp_path):
    """Only the layer's own files are shown; another layer's module is not."""
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(new_file_diff("rag/fusion/weights.py")))

    candidate = _generate(fake, cluster, member, trace)

    assert candidate.paths == ("rag/fusion/weights.py",)
    prompt = _request_text(fake)
    assert RERANKER_MARKER in prompt  # rag/rerank*/** is a reranking glob
    assert CHUNKER_MARKER not in prompt  # rag/chunker.py belongs to chunking


def test_target_files_are_ranked_by_diagnosis_relevance_and_truncated(tmp_path):
    """The open ``code`` layer ranks the module the diagnosis names first."""
    trace, cluster, member = seeded(tmp_path)
    draft = _draft(
        suggested_layer="code",
        root_cause="vector_store 写入前未校验向量维度，检索阶段向量不匹配。",
    )
    fake = FakeModel(model_reply(edit_file_diff()))

    # rag/reranker.py sits inside the code layer's rag/** glob, so the candidate
    # itself is valid; the assertions below are about the request it was built
    # from — which files the diagnosis selected, and how they were rendered.
    candidate = _generate(fake, cluster, member, trace, layer="code", drafts=[draft])
    assert candidate.paths == (TARGET_FILE,)

    prompt = _request_text(fake)
    selected = [line for line in prompt.split("\n") if line.startswith("### 文件 ")]
    assert len(selected) == 3  # one to three files, never the whole package
    assert selected[0].startswith("### 文件 rag/vector_store.py")
    assert VECTOR_STORE_MARKER in prompt
    vs_lines = len(Path("rag/vector_store.py").read_text(encoding="utf-8").splitlines())
    assert "前 400 行" in prompt
    assert f"共 {vs_lines} 行" in prompt
    assert TRUNCATED_AWAY_MARKER not in prompt


def test_prompt_and_metadata_carry_no_credentials(tmp_path, monkeypatch):
    trace, cluster, member = seeded(tmp_path)
    monkeypatch.setattr(settings, "deepseek_api_key", SECRET_SENTINEL)
    fake = FakeModel(model_reply(edit_file_diff()))
    generator = PatchGenerator(model_call=fake)

    candidate = generator.generate(
        cluster,
        [_draft()],
        layer="reranking",
        repo_root=REPO_ROOT,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )

    assert SECRET_SENTINEL not in _request_text(fake)
    assert SECRET_SENTINEL not in candidate.model_dump_json()
    assert SECRET_SENTINEL not in str(generator.last_meta)


def test_default_model_call_uses_an_isolated_client_with_the_configured_limits(monkeypatch, tmp_path):
    """Patch generation must not share the online generator's client.

    Same discipline as the phase-3/4 channels: its own client, its own timeout
    and a bounded ``max_tokens``, thinking disabled — a background generation
    call must never trip the online circuit breaker nor run unbilled.
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
                    SimpleNamespace(message=SimpleNamespace(content=model_reply(edit_file_diff())))
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            recorded["client"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    monkeypatch.setattr(settings, "deepseek_model", "deepseek/patch-probe")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://provider.example/v1")
    monkeypatch.setattr(settings, "deepseek_api_key", "probe-key")
    try:
        candidate = PatchGenerator(timeout_seconds=90.0, max_tokens=16000).generate(
            cluster,
            [_draft()],
            layer="reranking",
            repo_root=REPO_ROOT,
            observation_id=member["id"],
            cluster_id=cluster["id"],
            trace_store=trace,
        )
    finally:
        monkeypatch.setattr(openai, "OpenAI", real_client)

    assert candidate.paths == (TARGET_FILE,)
    assert recorded["client"]["timeout"] == 90.0
    assert recorded["model"] == "deepseek/patch-probe"
    assert recorded["temperature"] == PATCH_TEMPERATURE
    assert recorded["max_tokens"] == 16000
    assert recorded["extra_body"] == {"thinking": {"type": "disabled"}}


# ------------------------------------------------------------------
# The administrator's confirmed layer (design sections 6/8).
# ------------------------------------------------------------------


def test_an_administrator_confirmed_layer_overrides_the_draft_suggestion(tmp_path):
    """The human confirmation governs: a prompt-layer draft still yields the
    reranking patch the administrator confirmed."""
    trace, cluster, member = seeded(tmp_path)
    confirmed = {**cluster, "confirmed_layer": "reranking"}
    fake = FakeModel(model_reply(new_file_diff("rag/fusion/weights.py")))

    candidate = _generate(
        fake, confirmed, member, trace, drafts=[_draft(suggested_layer="prompt")]
    )

    assert candidate.layer == "reranking"
    assert candidate.paths == ("rag/fusion/weights.py",)


def test_a_confirmed_layer_blocks_a_patch_for_another_layer(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    confirmed = {**cluster, "confirmed_layer": "chunking"}
    fake = FakeModel(model_reply(edit_file_diff()))

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, confirmed, member, trace, layer="reranking")

    assert "chunking" in str(excinfo.value)
    assert excinfo.value.reason == "invalid"


def test_without_a_confirmation_the_draft_layer_still_has_to_match(tmp_path):
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(edit_file_diff()))

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace, layer="reranking", drafts=[_draft(suggested_layer="code")])

    assert "suggested_layer" in str(excinfo.value)


# ------------------------------------------------------------------
# Helpers bound to the module under test.
# ------------------------------------------------------------------


def repo_head() -> str:
    """The repository HEAD, read from git in the test's own subprocess."""
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def test_the_default_completion_budget_leaves_room_for_the_reasoning_cost():
    """默认完成预算必须给推理留足余量：``thinking: disabled`` 在这个 provider 上没生效。

    2026-10-02 实测（temperature 0.2、30k 字符提示词）：每次调用花 **5076–10259** 个推理
    token，正文在推理之后才输出；``max_tokens=16000`` 时 5 次尝试里 3 次只回 9 个字符、
    拿不到 unified diff。这条守卫把常量与构造器默认值绑在一起，防止被改回去。
    """
    import inspect

    from rag.evolution.patch_generator import PATCH_MAX_TOKENS, PatchGenerator

    assert PATCH_MAX_TOKENS >= 32000
    default = inspect.signature(PatchGenerator.__init__).parameters["max_tokens"].default
    assert default == PATCH_MAX_TOKENS


def test_the_prompt_keeps_tests_out_of_the_diff(tmp_path):
    """测试只进元数据、不进 diff：这是实测最多的拒绝原因。

    2026-10-02：同一诊断 3/3 次被拒于「diff 新建了 tests/test_spreadsheets_query.py」。
    规则 5 要求元数据里给 test_paths，模型把「要提供测试」误当成「往 diff 里加测试」，
    因此两处规则都写死了这个区分。
    """
    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel(model_reply(edit_file_diff()))

    _generate(fake, cluster, member, trace)

    prompt = _request_text(fake)
    assert "不得在 diff 里新建或修改" in prompt
    assert "不得新建任何文件" in prompt


# ------------------------------------------------------------------
# 异常栈最后一帧：选文件的事实依据（而不是按诊断措辞猜关键词）。
# ------------------------------------------------------------------


def test_the_exception_frame_file_outranks_the_keyword_ranking(tmp_path):
    """帧点名的文件必须被选中并排在第一位——即使关键词排序会把它挤出去。

    实测：cluster #30 的真实帧在 rag/pipeline.py（证据边界），但生成器按诊断措辞
    选中了 rag/agent.py，补丁因此打在症状上。
    """
    from rag.evolution.patch_generator import _select_target_files

    repo = tmp_path
    (repo / "rag").mkdir()
    for name in ("pipeline.py", "agent.py", "reranker.py"):
        (repo / "rag" / name).write_text("def placeholder():\n    return None\n", encoding="utf-8")
    files = ["rag/agent.py", "rag/pipeline.py", "rag/reranker.py"]

    without = _select_target_files(repo, files, ("agent", "router"))
    with_frame = _select_target_files(repo, files, ("agent", "router"), prefer="rag/pipeline.py")

    assert without[0][0] == "rag/agent.py"  # 关键词排序：agent 赢
    assert with_frame[0][0] == "rag/pipeline.py"  # 帧赢，且排在第一位
    assert with_frame[0][0] in {entry[0] for entry in with_frame}


def test_an_unknown_or_absent_frame_falls_back_to_keyword_ranking(tmp_path):
    """帧缺失或指向层外文件时不得改变既有排序（回退，而不是报错）。"""
    from rag.evolution.patch_generator import _select_target_files

    repo = tmp_path
    (repo / "rag").mkdir()
    for name in ("agent.py", "reranker.py"):
        (repo / "rag" / name).write_text("def placeholder():\n    return None\n", encoding="utf-8")
    files = ["rag/agent.py", "rag/reranker.py"]

    baseline = _select_target_files(repo, files, ("agent",))
    assert [entry[0] for entry in _select_target_files(repo, files, ("agent",), prefer="")] == [
        entry[0] for entry in baseline
    ]
    assert [entry[0] for entry in _select_target_files(repo, files, ("agent",), prefer="rag/nope.py")] == [
        entry[0] for entry in baseline
    ]


def test_the_frame_is_read_from_the_stored_observation():
    """帧从观测里读（``details_exception_frame``），没有就回退到关键词。"""
    from rag.evolution.patch_generator import _observation_exception_path

    class Store:
        def __init__(self, frame):
            self.frame = frame

        def get_observation(self, observation_id):
            return {"id": observation_id, "details_exception_frame": self.frame}

    assert (
        _observation_exception_path(Store("rag/pipeline.py:700:_finalize_rag_context"), 132)
        == "rag/pipeline.py"
    )
    assert _observation_exception_path(Store(None), 132) == ""
    assert _observation_exception_path(None, 132) == ""


def test_a_store_that_cannot_answer_is_not_a_failed_generation():
    """存储读失败不得让生成失败：回退关键词排序即可。"""
    from rag.evolution.patch_generator import _observation_exception_path

    class Broken:
        def get_observation(self, observation_id):
            raise RuntimeError("store exploded")

    assert _observation_exception_path(Broken(), 1) == ""


# ------------------------------------------------------------------
# AST 符号索引 + 靶心窗口（队列 4）：事实优先于关键词线索。
# ------------------------------------------------------------------


def test_a_target_known_to_live_far_into_a_file_is_shown_around_that_line(tmp_path):
    """靶心窗口：目标在第 901 行时，"前 400 行"什么也看不到——必须显示那一段。"""
    from rag.evolution.patch_generator import _select_target_files

    repo = tmp_path
    (repo / "rag").mkdir()
    body = [f"# filler {index}" for index in range(1, 1201)]
    body[900] = "def _finalize_rag_context():  # the failure lives here"
    (repo / "rag" / "pipeline.py").write_text("\n".join(body), encoding="utf-8")
    (repo / "rag" / "agent.py").write_text("def route_question():\n    return 'rag'\n", encoding="utf-8")

    targets = _select_target_files(
        repo,
        ["rag/agent.py", "rag/pipeline.py"],
        ("agent", "router"),
        prefer="rag/pipeline.py",
        focused=(("rag/pipeline.py", 901),),
    )

    first = targets[0]
    assert first[0] == "rag/pipeline.py"
    assert "_finalize_rag_context" in first[1]  # 窗口覆盖第 901 行
    assert "仅显示第" in first[4]  # 说明显示的是哪一段
    assert "1201" in first[4] or "共" in first[4]


def test_the_real_repository_index_resolves_the_symbols_a_diagnosis_names(tmp_path):
    """用真实仓库验收：中文诊断里点名的**真实**符号被解析到正确文件（虚构的不产生噪声）。"""
    from rag.evolution.symbols import SymbolIndex

    index = SymbolIndex.build(
        REPO_ROOT, ["rag/pipeline.py", "rag/retriever.py", "rag/agent.py"]
    )

    symbols = index.symbols_in_text(
        "异常发生在 _finalize_rag_context，与 _rrf_fuse 的排序有关，zzz_not_a_symbol 无关"
    )

    assert [symbol.path for symbol in symbols] == ["rag/pipeline.py", "rag/retriever.py"]
    assert symbols[1].qualname.endswith("_rrf_fuse")


def test_the_locate_note_names_the_frame_and_the_named_symbol():
    """定位块把"事实"放在文件正文之前：异常帧在哪、诊断在说哪个符号。"""
    from rag.evolution.patch_generator import _render_locate_note
    from rag.evolution.symbols import Symbol

    note = _render_locate_note(
        "rag/pipeline.py",
        745,
        (Symbol("rag/retriever.py", "Retriever._rrf_fuse", "method", 230, 266),),
    )

    assert "异常栈最后一帧：rag/pipeline.py:745" in note
    assert "Retriever._rrf_fuse" in note
    assert "rag/retriever.py:230-266" in note
    assert _render_locate_note("", None, ()) == ""

def test_the_prompt_lists_only_tests_that_really_exist(tmp_path):
    """实测动机：cluster 30 上模型 2/2 次点名了不存在的测试文件，被边界正确拒绝。

    提示词只给源文件、不说有哪些测试 ⇒ 只能猜。这里改为列出**真实存在**的文件名（只给名字，
    不含内容），且优先列出真的 import 了被改模块的测试。
    """
    from rag.evolution.patch_generator import _available_tests, _render_available_tests

    tests = _available_tests(Path(REPO_ROOT), ("rag/reranker.py",))

    assert "tests/test_reranker.py" in tests
    assert all((Path(REPO_ROOT) / name).is_file() for name in tests)
    rendered = _render_available_tests(tests)
    assert "只能从中选择" in rendered
    assert "tests/test_reranker.py" in rendered


def test_the_prompt_says_what_to_do_when_no_test_matches():
    from rag.evolution.patch_generator import _render_available_tests

    rendered = _render_available_tests(())

    assert "绝不编造" in rendered
    assert "不存在" in rendered or "确实存在" in rendered

# ------------------------------------------------------------------
# 带失败类别的一次重试（含真实调用计数）。
# ------------------------------------------------------------------


def test_a_rejected_diff_is_retried_once_with_the_failure_category(tmp_path):
    """实测动机：约每五次生成就有一次被边界拒绝；重试只带我们自己的类别，绝不回灌被拒输出。"""
    trace, cluster, member = seeded(tmp_path)
    bad = "这不是一个 diff"
    fake = FakeModel(bad, model_reply(edit_file_diff(TARGET_FILE)))
    generator = PatchGenerator(model_call=fake)

    candidate = generator.generate(
        cluster,
        [_draft()],
        layer="reranking",
        repo_root=REPO_ROOT,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )

    # 既有夹具产出的 diff 以 `--- a/` 开头（不带 diff --git 头），断言据此写。
    assert candidate.diff.startswith("--- a/")
    assert generator.model_calls == 2
    assert len(fake.requests) == 2
    note = fake.requests[1][-1]["content"]
    assert "上一次尝试被拒绝" in note
    assert "失败类别" in note
    assert bad not in note  # 被拒的输出绝不回灌


def test_two_rejections_raise_after_exactly_two_calls(tmp_path):
    from rag.evolution.patch_generator import PatchUnavailable

    trace, cluster, member = seeded(tmp_path)
    fake = FakeModel("还是不是 diff", "依然不是 diff")
    generator = PatchGenerator(model_call=fake)

    with pytest.raises(PatchUnavailable):
        generator.generate(
            cluster,
            [_draft()],
            layer="reranking",
            repo_root=REPO_ROOT,
            observation_id=member["id"],
            cluster_id=cluster["id"],
            trace_store=trace,
        )

    assert generator.model_calls == 2

def test_a_context_override_widens_the_window_and_says_so(tmp_path):
    """加大上下文这一档实验：只调「上限」是扩不出窗口的（窗宽 = min(上限, 半径*2+1)），

    所以 N>0 时半径取 N//2。默认 0 必须**完全保持**既有行为（既有用例覆盖了那一条）。
    """
    from rag.evolution.patch_generator import _select_target_files

    repo = tmp_path
    (repo / "rag").mkdir()
    body = [f"# filler {index}" for index in range(1, 3001)]
    body[2400] = "def _finalize_rag_context():  # the failure lives here"
    (repo / "rag" / "pipeline.py").write_text("\n".join(body), encoding="utf-8")

    narrow = _select_target_files(
        repo, ["rag/pipeline.py"], (), prefer="rag/pipeline.py", focused=(("rag/pipeline.py", 2401),)
    )[0]
    wide = _select_target_files(
        repo,
        ["rag/pipeline.py"],
        (),
        prefer="rag/pipeline.py",
        focused=(("rag/pipeline.py", 2401),),
        context_lines=1600,
    )[0]

    assert "_finalize_rag_context" in narrow[1] and "_finalize_rag_context" in wide[1]
    assert len(wide[1].splitlines()) > len(narrow[1].splitlines())
    assert len(wide[1].splitlines()) == 1600  # 上限即是窗宽（半径 800）
    # 聚焦目标的说明是**窗口**措辞（「仅显示第 A–B 行（共 N 行）」），不是「已截断：仅显示前 N 行」。
    assert "仅显示第" in wide[4] and "共 3000 行" in wide[4]

# ------------------------------------------------------------------
# edits 格式（patch-v5）：模型给精确替换块，我们用 difflib 拼 diff。
# ------------------------------------------------------------------


def test_structured_edits_assemble_a_diff_that_git_accepts():
    """核心主张：difflib 拼出来的 diff，hunk 头与上下文**构造上**自洽 ⇒ git 一定认。"""
    import subprocess

    from rag.evolution.patch_generator import edits_to_diff

    target = REPO_ROOT / "rag" / "reranker.py"
    first = target.read_text(encoding="utf-8").splitlines()[0]

    diff = edits_to_diff(
        [{"path": "rag/reranker.py", "old_text": first, "new_text": first + "  # 结构化编辑"}],
        repo_root=REPO_ROOT,
        layer="reranking",
    )

    assert diff.startswith("diff --git a/rag/reranker.py b/rag/reranker.py")
    assert "@@" in diff
    check = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn", "-"],
        cwd=str(REPO_ROOT),
        input=diff,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr


def test_structured_edits_refuse_with_content_free_reasons():
    from rag.evolution.patch_generator import PatchUnavailable, edits_to_diff

    with pytest.raises(PatchUnavailable) as missing:
        edits_to_diff(
            [{"path": "rag/reranker.py", "old_text": "绝不存在的字符串zzz", "new_text": "x"}],
            repo_root=REPO_ROOT,
            layer="reranking",
        )
    assert missing.value.detail.endswith("old_text not found")

    with pytest.raises(PatchUnavailable) as outside:
        edits_to_diff(
            [{"path": "rag/pipeline.py", "old_text": "import", "new_text": "x"}],
            repo_root=REPO_ROOT,
            layer="reranking",
        )
    assert outside.value.detail.endswith("path outside the layer")


def test_structured_edits_reply_becomes_a_candidate(tmp_path):
    """整条路径：edits 格式的 JSON 回复 ⇒ 我们自己拼 diff ⇒ 过的还是同一个边界。"""
    import json as _json

    trace, cluster, member = seeded(tmp_path)
    target = REPO_ROOT / "rag" / "reranker.py"
    first = target.read_text(encoding="utf-8").splitlines()[0]
    reply = _json.dumps(
        {
            "edits": [
                {"path": "rag/reranker.py", "old_text": first, "new_text": first + "  # 结构化编辑"}
            ],
            "test_paths": [TARGET_TEST],
            "rationale": "同义对齐",
            "expected_effect": "Top-5 命中回升",
            "confidence": "low",
        },
        ensure_ascii=False,
    )
    fake = FakeModel(reply)
    generator = PatchGenerator(model_call=fake, patch_format="edits")

    candidate = generator.generate(
        cluster,
        [_draft()],
        layer="reranking",
        repo_root=REPO_ROOT,
        observation_id=member["id"],
        cluster_id=cluster["id"],
        trace_store=trace,
    )

    assert candidate.diff.startswith("diff --git a/rag/reranker.py")
    assert "# 结构化编辑" in candidate.diff
