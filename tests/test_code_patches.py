"""Task 1: code-patch candidate contracts and the path boundary.

A code-patch candidate carries a unified diff, so unlike the question-set
payload (pure text) its boundary is a **path boundary**: which layer may touch
which files (``LAYER_PATH_GLOBS``), what nothing may ever touch
(``FORBIDDEN_GLOBS``), how large a diff may be, and whether the repository the
patch names can still take it (``git apply --check``).

The layer table and the blacklist are the plan's verbatim lists; these tests pin
them, every rejection reason class, and both directions of the git check against
the real repository. Diff fixtures are LF-normalized because this repository's
git runs with ``core.autocrlf=true`` (the index is LF while the working tree is
CRLF), so an LF diff is what ``git apply`` accepts here.
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest
from pydantic import ValidationError

from rag.evolution.candidates import (
    JevQuestionSetCandidate,
    PayloadBoundaryError,
    validate_payload,
)
from rag.evolution.code_patches import (
    FORBIDDEN_GLOBS,
    LAYER_PATH_GLOBS,
    CodePatchCandidate,
    validate_patch,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "a" * 40
PROBE_COMMENT = "# code-patch boundary probe (comment only)"
EXISTING_TARGET = "rag/reranker.py"
EXISTING_TEST = "tests/test_reranker.py"


# ------------------------------------------------------------------
# Diff fixtures: real, parseable diffs against the real working tree.
# ------------------------------------------------------------------


def new_file_diff(path: str) -> str:
    """A ``git apply --check``-clean diff that creates ``path``.

    A new file is legal inside an allowed glob (the blacklist restricts what a
    patch may *change*, and ``paths`` already names every file in the diff), so
    this is the shape used for layer whitelists that point at modules this
    repository does not have yet.
    """
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1,2 @@\n"
        f"+{PROBE_COMMENT}\n"
        "+value = 1\n"
    )


def edit_file_diff(path: str) -> str:
    """A real diff: one comment line appended to an existing file."""
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    edited = [*lines, f"{PROBE_COMMENT}\n"]
    return "".join(
        difflib.unified_diff(lines, edited, fromfile=f"a/{path}", tofile=f"b/{path}")
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


def diff_for(path: str) -> str:
    """Edit ``path`` when it exists, create it otherwise."""
    if (REPO_ROOT / path).is_file():
        return edit_file_diff(path)
    return new_file_diff(path)


def patch_kwargs(**kw) -> dict:
    """A valid reranking candidate over ``rag/reranker.py``; ``kw`` edits it."""
    data = dict(
        artifact_type="code_patch",
        layer="reranking",
        paths=(EXISTING_TARGET,),
        diff=edit_file_diff(EXISTING_TARGET),
        test_paths=(EXISTING_TEST,),
        rationale="词面撞名候选淹没相关段落，回放报告 B 类可复现",
        expected_effect="Top-5 命中回升，路线不变",
        rollback_ref={"base_commit": BASE_COMMIT},
        confidence="medium",
    )
    data.update(kw)
    return data


def patch_candidate(**kw) -> CodePatchCandidate:
    return CodePatchCandidate(**patch_kwargs(**kw))


def reject_reason(candidate: CodePatchCandidate) -> str:
    """Run the boundary check and return the rejection reason."""
    with pytest.raises(PayloadBoundaryError) as excinfo:
        validate_patch(candidate, repo_root=REPO_ROOT)
    return str(excinfo.value)


# ------------------------------------------------------------------
# The frozen contract.
# ------------------------------------------------------------------


def test_candidate_carries_the_patch_contract():
    candidate = patch_candidate()
    assert candidate.artifact_type == "code_patch"
    assert candidate.layer == "reranking"
    assert candidate.paths == (EXISTING_TARGET,)
    assert candidate.diff
    assert candidate.test_paths == (EXISTING_TEST,)
    assert candidate.rollback_ref["base_commit"] == BASE_COMMIT
    assert candidate.confidence == "medium"


def test_candidate_is_frozen():
    candidate = patch_candidate()
    with pytest.raises(ValidationError):
        candidate.diff = ""


def test_candidate_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        CodePatchCandidate(**patch_kwargs(secret="token"))


def test_paths_and_test_paths_accept_lists():
    candidate = patch_candidate(paths=[EXISTING_TARGET], test_paths=[EXISTING_TEST])
    assert candidate.paths == (EXISTING_TARGET,)
    assert candidate.test_paths == (EXISTING_TEST,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_type", "jev_question_set"),
        ("layer", "prompt"),
        ("layer", "retrieval_config"),
        ("confidence", "certain"),
    ],
)
def test_contract_rejects_out_of_enum_values(field, value):
    with pytest.raises(ValidationError):
        CodePatchCandidate(**patch_kwargs(**{field: value}))


@pytest.mark.parametrize("layer", sorted(LAYER_PATH_GLOBS))
def test_every_declared_layer_is_constructible(layer):
    assert patch_candidate(layer=layer).layer == layer


# ------------------------------------------------------------------
# The verbatim plan tables.
# ------------------------------------------------------------------


def test_layer_table_matches_the_plan_verbatim():
    assert LAYER_PATH_GLOBS == {
        "chunking": ("rag/chunk*/**", "rag/chunker*/**"),
        "reranking": ("rag/rerank*/**", "rag/rrf*/**", "rag/fusion*/**"),
        "parser": ("rag/parsers/**", "rag/document_sources/**", "rag/extract*/**"),
        "tool_workflow": (
            "rag/agent*.py",
            "rag/tools/**",
            "rag/coordinator*/**",
            "rag/spreadsheets/**",
            "rag/structured_query*/**",
        ),
        "code": ("rag/**",),
    }


def test_blacklist_matches_the_plan_verbatim():
    assert FORBIDDEN_GLOBS == (
        "rag/permissions.py",
        "rag/api_security.py",
        "rag/org_auth/**",
        "rag/auth.py",
        "rag/user_db.py",
        "rag/evolution/**",
        "rag/wiki/**",
        "tests/**",
        "**/conftest.py",
        "scripts/**",
        "config.py",
        "frontend/**",
        ".github/**",
        "docs/superpowers/**",
        "**/.env*",
    )


# ------------------------------------------------------------------
# A small real patch is accepted.
# ------------------------------------------------------------------


def test_a_comment_only_edit_of_an_allowed_module_is_accepted():
    assert validate_patch(patch_candidate(), repo_root=REPO_ROOT) is None


def test_a_new_file_inside_an_allowed_glob_is_accepted():
    candidate = patch_candidate(
        layer="chunking",
        paths=("rag/chunking/splitter.py",),
        diff=new_file_diff("rag/chunking/splitter.py"),
    )
    assert validate_patch(candidate, repo_root=REPO_ROOT) is None


def test_five_files_are_within_the_quota():
    paths = tuple(f"rag/fusion/module_{index}.py" for index in range(5))
    diff = "".join(new_file_diff(path) for path in paths)
    assert (
        validate_patch(patch_candidate(layer="reranking", paths=paths, diff=diff), repo_root=REPO_ROOT)
        is None
    )


def test_three_hundred_changed_lines_are_within_the_quota():
    body = "".join(f"+value_{index} = {index}\n" for index in range(300))
    diff = "--- a/rag/fusion/weights.py\n+++ b/rag/fusion/weights.py\n@@ -0,0 +1,300 @@\n" + body
    candidate = patch_candidate(
        layer="reranking", paths=("rag/fusion/weights.py",), diff=diff
    )
    assert validate_patch(candidate, repo_root=REPO_ROOT) is None


# ------------------------------------------------------------------
# Layer whitelist: hits and misses.
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "layer,path,test_paths",
    [
        ("chunking", "rag/chunker.py", (EXISTING_TEST,)),
        ("chunking", "rag/chunking/splitter.py", (EXISTING_TEST,)),
        ("reranking", "rag/reranker.py", (EXISTING_TEST,)),
        ("reranking", "rag/rrf_fusion.py", (EXISTING_TEST,)),
        ("reranking", "rag/fusion/weights.py", (EXISTING_TEST,)),
        ("parser", "rag/document_sources/base.py", (EXISTING_TEST,)),
        ("parser", "rag/parsers/pdf.py", (EXISTING_TEST,)),
        ("parser", "rag/extractor.py", (EXISTING_TEST,)),
        ("tool_workflow", "rag/agent.py", (EXISTING_TEST,)),
        ("tool_workflow", "rag/tools/registry.py", (EXISTING_TEST,)),
        ("tool_workflow", "rag/spreadsheets/audit.py", (EXISTING_TEST,)),
        ("tool_workflow", "rag/coordinator_loop.py", (EXISTING_TEST,)),
        ("tool_workflow", "rag/structured_query_planner.py", (EXISTING_TEST,)),
        # C-10：retriever 承载 ACL 调用点，改它必须点名一条权限用例（否则层 1 拒绝）。
        ("code", "rag/retriever.py", ("tests/test_retriever.py", "tests/test_permissions.py")),
        ("code", "rag/deep/nested/module.py", (EXISTING_TEST,)),
    ],
)
def test_layer_whitelist_admits_its_own_paths(layer, path, test_paths):
    candidate = patch_candidate(layer=layer, paths=(path,), diff=diff_for(path), test_paths=test_paths)
    assert validate_patch(candidate, repo_root=REPO_ROOT) is None


@pytest.mark.parametrize(
    "layer,path",
    [
        ("chunking", "rag/reranker.py"),
        ("chunking", "rag/agent.py"),
        ("reranking", "rag/chunker.py"),
        ("reranking", "rag/retriever.py"),
        ("parser", "rag/reranker.py"),
        ("parser", "rag/tools/registry.py"),
        ("tool_workflow", "rag/retriever.py"),
        ("tool_workflow", "rag/document_sources/base.py"),
        ("code", "pyproject.toml"),
        ("code", "prompts/agent.txt"),
    ],
)
def test_layer_whitelist_refuses_paths_of_another_layer(layer, path):
    candidate = patch_candidate(
        layer=layer, paths=(path,), diff=new_file_diff(path), test_paths=(EXISTING_TEST,)
    )
    reason = reject_reason(candidate)
    assert path in reason
    assert "forbidden" not in reason


def test_code_layer_refuses_a_path_outside_rag():
    reason = reject_reason(
        patch_candidate(layer="code", paths=("docs/readme.md",), diff=new_file_diff("docs/readme.md"))
    )
    assert "docs/readme.md" in reason
    assert "rag/" in reason


# ------------------------------------------------------------------
# Blacklist: every documented prefix, checked before the whitelist.
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "rag/permissions.py",
        "rag/api_security.py",
        "rag/org_auth/members.py",
        "rag/auth.py",
        "rag/user_db.py",
        "rag/evolution/code_patches.py",
        "rag/evolution/candidate_store.py",
        "rag/evolution/candidate_gates.py",
        "rag/wiki/store.py",
        "tests/test_reranker.py",
        "tests/conftest.py",
        "tests/evolution/test_store.py",
        "scripts/benchmark_jev_retrieval.py",
        "config.py",
        "frontend/src/views/EvolutionCenterView.vue",
        ".github/workflows/ci.yml",
        "docs/superpowers/plans/2026-09-27-assisted-self-evolution-code-patches.md",
        ".env",
        ".env.production",
        "rag/.env.local",
        "deploy/settings/.env",
    ],
)
def test_blacklisted_paths_are_refused(path):
    candidate = patch_candidate(
        layer="code", paths=(path,), diff=new_file_diff(path), test_paths=(EXISTING_TEST,)
    )
    reason = reject_reason(candidate)
    assert path in reason
    assert "forbidden" in reason


def test_rejection_names_the_matched_blacklist_glob():
    """Self-modification is refused by name: a patch may not touch ``rag/evolution/**``."""
    reason = reject_reason(
        patch_candidate(
            layer="code",
            paths=("rag/evolution/code_patches.py",),
            diff=new_file_diff("rag/evolution/code_patches.py"),
        )
    )
    assert "rag/evolution/**" in reason
    assert "rag/evolution/code_patches.py" in reason


def test_a_blacklisted_path_is_refused_before_the_layer_whitelist():
    """The blacklist runs first: the reason names a glob, never a layer complaint."""
    reason = reject_reason(
        patch_candidate(
            layer="chunking",
            paths=("rag/evolution/code_patches.py",),
            diff=new_file_diff("rag/evolution/code_patches.py"),
        )
    )
    assert "rag/evolution/**" in reason
    assert "chunking" not in reason


# ------------------------------------------------------------------
# test_paths: the verification targets a patch must name.
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "test_paths",
    [
        ("rag/test_reranker.py",),
        ("tests/reranker.py",),
        ("tests/reranker_test.py",),
        ("tests/conftest.py",),
        ("tests/test_missing_module_xyz.py",),
        (),
    ],
)
def test_invalid_test_paths_are_refused(test_paths):
    reason = reject_reason(patch_candidate(test_paths=test_paths))
    assert "test_paths" in reason or any(path in reason for path in test_paths)


def test_a_patch_must_name_at_least_one_file():
    assert "paths" in reject_reason(patch_candidate(paths=()))


# ------------------------------------------------------------------
# Quotas: files, changed lines, binary, deletion, unknown files.
# ------------------------------------------------------------------


def test_more_than_five_files_is_refused():
    paths = tuple(f"rag/fusion/module_{index}.py" for index in range(6))
    diff = "".join(new_file_diff(path) for path in paths)
    reason = reject_reason(patch_candidate(layer="reranking", paths=paths, diff=diff))
    assert "6" in reason
    assert "5" in reason


def test_more_than_three_hundred_changed_lines_is_refused():
    body = "".join(f"+value_{index} = {index}\n" for index in range(301))
    diff = "--- a/rag/fusion/weights.py\n+++ b/rag/fusion/weights.py\n@@ -0,0 +1,301 @@\n" + body
    candidate = patch_candidate(
        layer="reranking", paths=("rag/fusion/weights.py",), diff=diff
    )
    reason = reject_reason(candidate)
    assert "301" in reason
    assert "300" in reason


@pytest.mark.parametrize(
    "diff",
    [
        "diff --git a/rag/fusion/logo.png b/rag/fusion/logo.png\n"
        "new file mode 100644\n"
        "index 0000000..0000000\n"
        "GIT binary patch\n"
        "literal 4\n"
        "zc$@\n"
        "\n",
        "diff --git a/rag/fusion/logo.png b/rag/fusion/logo.png\n"
        "index 0000000..0000000 100644\n"
        "Binary files a/rag/fusion/logo.png and b/rag/fusion/logo.png differ\n",
    ],
)
def test_a_binary_diff_is_refused(diff):
    candidate = patch_candidate(layer="reranking", paths=("rag/fusion/logo.png",), diff=diff)
    reason = reject_reason(candidate)
    assert "binary" in reason.lower()


def test_a_diff_that_deletes_a_file_is_refused():
    diff = (
        "diff --git a/rag/reranker.py b/rag/reranker.py\n"
        "deleted file mode 100644\n"
        "--- a/rag/reranker.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-line one\n"
        "-line two\n"
    )
    reason = reject_reason(patch_candidate(diff=diff))
    assert "delet" in reason.lower()


def test_a_deletion_without_the_extended_header_is_refused():
    diff = "--- a/rag/reranker.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    reason = reject_reason(patch_candidate(diff=diff))
    assert "delet" in reason.lower()


def test_a_diff_touching_a_path_outside_paths_is_refused():
    diff = edit_file_diff(EXISTING_TARGET) + new_file_diff("rag/fusion/extra.py")
    reason = reject_reason(patch_candidate(diff=diff))
    assert "rag/fusion/extra.py" in reason


def test_a_rename_between_named_paths_is_accepted():
    """A 100%-similarity rename has no diff headers, so its endpoints are pinned."""
    candidate = patch_candidate(
        paths=("rag/reranker.py", "rag/reranker_alt.py"),
        diff=(
            "diff --git a/rag/reranker.py b/rag/reranker_alt.py\n"
            "similarity index 100%\n"
            "rename from rag/reranker.py\n"
            "rename to rag/reranker_alt.py\n"
        ),
    )
    assert validate_patch(candidate, repo_root=REPO_ROOT) is None


def test_a_rename_naming_a_path_outside_paths_is_refused():
    candidate = patch_candidate(
        paths=("rag/reranker.py",),
        diff=(
            "diff --git a/rag/reranker.py b/rag/reranker_alt.py\n"
            "similarity index 100%\n"
            "rename from rag/reranker.py\n"
            "rename to rag/reranker_alt.py\n"
        ),
    )
    reason = reject_reason(candidate)
    assert "rag/reranker_alt.py" in reason


def test_a_mode_only_diff_naming_a_foreign_path_is_refused():
    candidate = patch_candidate(
        paths=("rag/reranker.py",),
        diff=(
            "diff --git a/rag/permissions.py b/rag/permissions.py\n"
            "old mode 100644\n"
            "new mode 100755\n"
        ),
    )
    reason = reject_reason(candidate)
    assert "rag/permissions.py" in reason


# ------------------------------------------------------------------
# git apply --check, both directions, on the real repository.
# ------------------------------------------------------------------


def test_git_apply_check_accepts_a_real_diff():
    assert validate_patch(patch_candidate(), repo_root=REPO_ROOT) is None


def test_git_apply_check_refuses_a_diff_that_does_not_apply():
    candidate = patch_candidate(diff=non_applying_diff(EXISTING_TARGET))
    reason = reject_reason(candidate)
    assert reason == "git apply --check failed: does_not_apply"
    assert "preimage line" not in reason


# ------------------------------------------------------------------
# rollback_ref and path normalization.
# ------------------------------------------------------------------


@pytest.mark.parametrize("base_commit", ["", "abc", "z" * 40, "A" * 40, "a" * 39, "a" * 41])
def test_base_commit_must_be_forty_hex_characters(base_commit):
    reason = reject_reason(patch_candidate(rollback_ref={"base_commit": base_commit}))
    assert "base_commit" in reason
    if base_commit:  # an empty string is a substring of every reason
        assert base_commit not in reason


def test_rollback_ref_must_carry_a_base_commit():
    assert "base_commit" in reject_reason(patch_candidate(rollback_ref={}))


@pytest.mark.parametrize(
    "raw", ["rag\\reranker.py", "./rag/reranker.py", "rag/./reranker.py"]
)
def test_paths_are_normalized_to_posix_form(raw):
    candidate = patch_candidate(paths=(raw,), diff=edit_file_diff(EXISTING_TARGET))
    assert validate_patch(candidate, repo_root=REPO_ROOT) is None


@pytest.mark.parametrize(
    "path", ["rag/../config.py", "../outside.py", "/etc/passwd", "rag/../../etc/passwd"]
)
def test_traversal_and_absolute_paths_are_refused(path):
    reason = reject_reason(
        patch_candidate(paths=(path,), diff=new_file_diff(path))
    )
    assert path in reason


# ------------------------------------------------------------------
# The question-set sibling contract stays where it belongs.
# ------------------------------------------------------------------


def test_the_question_set_contract_admits_the_code_patch_artifact_type():
    candidate = JevQuestionSetCandidate(
        artifact_type="code_patch",
        target="preflight",
        payload={"intent": {"instructions": "x"}},
        rationale="共享候选记录类型，artifact_type 区分补丁与问题集",
        expected_effect="补丁走 validate_patch",
        rollback_ref={"question_set_version": "preflight-v1", "payload_sha256": "a" * 64},
        confidence="low",
    )
    assert candidate.artifact_type == "code_patch"


def test_validate_payload_serves_question_sets_only():
    """A code patch is bounded by ``validate_patch``; the payload gate refuses it."""
    with pytest.raises(ValueError) as excinfo:
        validate_payload(
            "preflight",
            {"intent": {"instructions": "x"}},
            {"intent": {}},
            artifact_type="code_patch",
        )
    error = excinfo.value
    assert not isinstance(error, PayloadBoundaryError)
    assert "code_patch" in str(error)
    assert "validate_patch" in str(error)


# ------------------------------------------------------------------
# C-10 (2026-10-22): the ACL/audit call sites are a path-level red line.
# ------------------------------------------------------------------


def test_the_acl_requirement_is_derived_from_the_changed_files():
    """派生规则本身：命中哪个调用点文件，就要求它对应的权限用例；别的层不受影响。"""
    from rag.evolution.code_patches import acl_call_site_paths, required_acl_test_paths

    assert required_acl_test_paths(("rag/reranker.py", "rag/chunker.py")) == ()
    assert acl_call_site_paths(("rag/reranker.py", "rag/pipeline.py")) == ("rag/pipeline.py",)

    pipeline = required_acl_test_paths(("rag/pipeline.py",))
    assert "tests/test_permissions.py" in pipeline
    assert "tests/test_api_security.py" in pipeline

    guard = required_acl_test_paths(("rag/guard.py",))
    assert guard == ("tests/test_permissions.py",)

    retriever = required_acl_test_paths(("rag/retriever.py",))
    assert retriever == ("tests/test_permissions.py",)


def test_a_patch_touching_an_acl_call_site_must_name_a_permission_test():
    """pipeline 承载 ACL 过滤：改它却不点名权限用例 ⇒ 层 1 拒绝（不是警告）。"""
    reason = reject_reason(
        patch_candidate(
            layer="code",
            paths=("rag/pipeline.py",),
            diff=edit_file_diff("rag/pipeline.py"),
            test_paths=("tests/test_pipeline.py",),
        )
    )

    assert "ACL" in reason
    assert "rag/pipeline.py" in reason
    assert "tests/test_permissions.py" in reason
    assert "tests/test_api_security.py" in reason


def test_an_acl_patch_that_names_the_permission_test_is_accepted():
    """要求是"点名权限用例"，不是"不许改 pipeline"：带了权限用例的补丁照常放行。"""
    candidate = patch_candidate(
        layer="code",
        paths=("rag/pipeline.py",),
        diff=edit_file_diff("rag/pipeline.py"),
        test_paths=("tests/test_pipeline.py", "tests/test_api_security.py"),
    )

    assert validate_patch(candidate, repo_root=REPO_ROOT) is None


def test_the_acl_check_runs_after_the_layer_boundary():
    """层级先判：reranking 层点 pipeline 得到的是层级拒绝（不会被 ACL 规则顶掉）。"""
    reason = reject_reason(
        patch_candidate(
            layer="reranking",
            paths=("rag/pipeline.py",),
            diff=new_file_diff("rag/pipeline.py"),
            test_paths=(EXISTING_TEST,),
        )
    )

    assert "outside the reranking layer" in reason
    assert "forbidden" not in reason
