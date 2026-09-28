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

    assert PATCH_PROMPT_VERSION == "patch-v1"
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
    fake = FakeModel(model_reply(non_applying_diff(TARGET_FILE)))

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

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
    fake = FakeModel(reply)

    with pytest.raises(PatchUnavailable) as excinfo:
        _generate(fake, cluster, member, trace)

    assert excinfo.value.reason == "invalid"
    assert len(fake.requests) == 1


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
    assert "诊断提示词/候选提示词版本：patch-v1" in prompt
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
    # A 558-line file is truncated to the first 400 lines, and says so.
    assert "前 400 行" in prompt
    assert "共 558 行" in prompt
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
