"""Code-patch candidate contracts and the path boundary (phase 5).

Where the question-set payload is a pure text variant, a code patch carries a
unified diff, so its boundary is a **path boundary** (design §2): which layer may
open which files, and what nothing may ever open. This module owns that boundary
and nothing else:

* :class:`CodePatchCandidate` — the frozen contract a generated diff must fit;
  ``extra="forbid"`` keeps a stray field (a chunk text, a credential) a
  construction error, exactly like ``JevQuestionSetCandidate``.
* :data:`LAYER_PATH_GLOBS` / :data:`FORBIDDEN_GLOBS` — the plan's verbatim
  tables. Every path is checked against the blacklist **first**, then against
  its layer's whitelist, so an allowed layer can never reopen a forbidden file.
* :func:`validate_patch` — the deterministic layer-1 gate: shape, path boundary,
  ``test_paths`` existence, the diff quotas, and a real ``git apply --check``
  against the repository the patch names (read-only, on stdin, no temp file).

Rejection reasons carry paths, globs, counts and git categories only — never
diff content — so a rejected patch cannot leak file content into a log, a gate
note, or an API error. The module is pure apart from that one subprocess: it
never writes to the working tree it checks against.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rag.evolution.candidates import PayloadBoundaryError

#: The artifact type this module serves (design §2 stage 2/3).
ARTIFACT_TYPE = "code_patch"

#: The five evolution layers a patch may address, most closed first (design §2).
LAYERS: tuple[str, ...] = ("chunking", "reranking", "parser", "tool_workflow", "code")

#: Layer → the path globs a candidate for that layer may touch. Verbatim from the
#: plan (Task 1 Interfaces); ``code`` is the open layer (``rag/**``) minus the
#: blacklist.
LAYER_PATH_GLOBS: dict[str, tuple[str, ...]] = {
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

#: Glob prefixes nothing may ever touch (plan Global Constraints): the security,
#: permission and user-data modules, the evolution package itself (no
#: self-modification), the test baseline and its conftest, the acceptance
#: scripts, the release-gate config, the frontend, CI, the design documents and
#: any ``.env*``/secret file.
FORBIDDEN_GLOBS: tuple[str, ...] = (
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

#: ACL/审计红线（C-10，2026-10-22）：这些文件承载权限边界的**调用点**，而它们不在黑名单里
#: ——合法的检索/排序/防注入改动经常就在这里。只按文件名划红线有一个盲区：补丁可以改
#: ``rag/pipeline.py`` 把 ``_post_rank_acl`` 删掉，而不碰任何一个"禁止"模块，于是边界静默失效。
#:
#: 因此对每个被改动的调用点文件，**从它派生**出必须点名的权限用例：补丁的 ``test_paths`` 里
#: 至少出现其中一条，否则层 1 直接拒绝（fail-closed）。这不是"要求它一定通过"——沙箱会真的
#: 跑这些用例——而是"不许在没有权限证据的情况下改这条路径"。
ACL_CALL_SITE_TESTS: dict[str, tuple[str, ...]] = {
    "rag/pipeline.py": ("tests/test_permissions.py", "tests/test_api_security.py"),
    "rag/guard.py": ("tests/test_permissions.py",),
    "rag/retriever.py": ("tests/test_permissions.py",),
}

#: The permission tests the fixed regression net always runs (C-10). They are the offline proof
#: that the ACL boundary still holds, and layer 1 refuses any diff that could rewrite ``tests/**``,
#: so a candidate cannot shrink them.
PERMISSION_REGRESSION_TESTS: tuple[str, ...] = (
    "tests/test_permissions.py",
    "tests/test_api_security.py",
)

# Quotas: one surgical patch at a time.
MAX_PATCH_FILES = 5
MAX_PATCH_CHANGED_LINES = 300

#: ``git apply --check`` is bounded, so a hung git cannot hold a gate round.
GIT_APPLY_TIMEOUT_S = 60.0

_GIT_COMMAND = ("git", "apply", "--check", "--whitespace=nowarn", "-")
_BASE_COMMIT_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z")
# Markers git emits for content that is not text.
_BINARY_MARKERS = ("GIT binary patch", "Binary files")
# First-line stderr phrases mapped to a short category. Only the first line is
# read, and only the mapped category leaves this function: the reason a diff was
# refused never carries the diff's own content.
_GIT_ERROR_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("patch failed", "does_not_apply"),
    ("does not apply", "does_not_apply"),
    ("already exists", "already_exists"),
    ("no such file", "missing_target"),
    ("no valid patches", "no_valid_patch"),
    ("corrupt patch", "corrupt_patch"),
    ("unrecognized input", "unrecognized_input"),
)


class CodePatchCandidate(BaseModel):
    """One code-patch candidate: a bounded unified diff over allowed files.

    ``extra="forbid"`` makes an unknown field (a chunk text, an answer, a
    credential) a construction error, exactly like ``DiagnosticDraft``;
    ``frozen`` keeps a validated candidate immutable for the gates that
    re-check it. ``rollback_ref`` carries the ``base_commit`` the diff applies
    to, so a rollback is a checkout rather than a guess.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_type: Literal["code_patch"]
    layer: Literal["chunking", "reranking", "parser", "tool_workflow", "code"]
    paths: tuple[str, ...]
    diff: str
    test_paths: tuple[str, ...]
    rationale: str
    expected_effect: str
    rollback_ref: dict
    confidence: Literal["low", "medium", "high"]


# ------------------------------------------------------------------
# Path normalization and glob matching.
# ------------------------------------------------------------------


def _normalize_path(raw: str) -> str:
    """Canonical POSIX form of one candidate path, or a boundary error.

    Backslashes and ``.`` segments normalize away (a Windows-style path names the
    same file). An absolute path or a ``..`` segment is refused: either could
    address a file outside the repository the diff will be applied to, which is
    how a blacklist prefix would be walked around.
    """
    posix = PurePosixPath(str(raw).replace("\\", "/"))
    normalized = posix.as_posix()
    if not normalized or normalized == ".":
        raise PayloadBoundaryError(f"path is empty: {raw!r}")
    if normalized.startswith("/") or re.match(r"\A[A-Za-z]:", normalized) or ".." in posix.parts:
        raise PayloadBoundaryError(
            f"path must be repository-relative and traversal-free: {normalized}"
        )
    return normalized


def _glob_patterns(glob: str) -> tuple[str, ...]:
    """The fnmatch patterns one plan glob means.

    ``fnmatch`` has no ``**`` — its ``*`` already crosses ``/``. Two git-style
    conveniences are restored so the table reads the way it was written: a
    trailing ``/**`` also admits the prefix itself (``rag/rerank*/**`` covers
    ``rag/reranker.py``, which the plan's own end-to-end reranking case needs),
    and a leading ``**/`` also admits the name at the repository root
    (``**/.env*`` covers ``.env``).
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


def _forbidden_glob(path: str, *, tests_are_targets: bool = False) -> str | None:
    """The blacklist glob ``path`` hits, if any.

    ``tests_are_targets`` lifts ``tests/**`` for ``test_paths``: pointing a
    verification test at ``tests/`` is what a test path is *for*, while the same
    glob still refuses a patch that would rewrite the test baseline. Other
    exceptions survive — a conftest is not a test file a patch may target.
    """
    for glob in FORBIDDEN_GLOBS:
        if tests_are_targets and glob == "tests/**":
            continue
        if any(fnmatch.fnmatch(path, pattern) for pattern in _glob_patterns(glob)):
            return glob
    return None


def acl_call_site_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """The touched paths that carry an ACL/audit call site, in order (C-10)."""
    return tuple(
        dict.fromkeys(path for path in paths if path in ACL_CALL_SITE_TESTS)
    )


def required_acl_test_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """The permission tests derived from the files this patch changes (C-10).

    Empty when the patch touches no ACL/audit call site — rankers, chunkers, parsers and tools are
    their own layer and are not this red line. When it does, the answer is the union of that file's
    ``ACL_CALL_SITE_TESTS`` entries, in table order: the caller (layer 1, and the generator's prompt)
    uses it as "at least one of these must be in ``test_paths``".
    """
    required: list[str] = []
    for path in acl_call_site_paths(paths):
        for test in ACL_CALL_SITE_TESTS[path]:
            if test not in required:
                required.append(test)
    return tuple(required)


# ------------------------------------------------------------------
# Diff parsing.
# ------------------------------------------------------------------


def _header_target(line: str) -> str | None:
    """The path one ``---``/``+++`` header names; ``None`` for ``/dev/null``."""
    target = line[4:].split("\t", 1)[0].strip().strip('"')
    if target == "/dev/null":
        return None
    return target[2:] if target.startswith(("a/", "b/")) else target


def _header_pairs(lines: list[str]) -> list[tuple[int, int]]:
    """Index pairs of the ``---``/``+++`` file headers of a unified diff.

    Git emits the two header lines of one file adjacently, so adjacency is what
    tells a header apart from a hunk line that merely starts with ``--`` or
    ``++``.
    """
    return [
        (index, index + 1)
        for index in range(len(lines) - 1)
        if lines[index].startswith("--- ") and lines[index + 1].startswith("+++ ")
    ]


def _diff_files(diff: str) -> list[tuple[str | None, str | None]]:
    """One ``(source, target)`` pair per file in the diff, in diff order.

    A null side means ``/dev/null`` there: a null *source* is a file the patch
    creates, a null *target* is a file the patch deletes.
    """
    lines = diff.splitlines()
    pairs: list[tuple[str | None, str | None]] = []
    for old_index, new_index in _header_pairs(lines):
        source = _header_target(lines[old_index])
        target = _header_target(lines[new_index])
        pairs.append((source, target))
    return pairs


def _diff_header_paths(diff: str) -> list[str]:
    """Every file a diff touches, in diff order; ``/dev/null`` is not a file."""
    paths: list[str] = []
    for source, target in _diff_files(diff):
        paths.extend(path for path in (source, target) if path is not None)
    return paths


def _diff_renamed_paths(diff: str) -> list[str]:
    """Every path a rename or copy moves content between.

    A 100%-similarity rename carries no ``---``/``+++`` headers and no changed
    lines, so without this it would be the one way to name a file the header
    check never sees — including a way to move content *into* a blacklisted path.
    Both endpoints belong to ``paths`` like any other file the diff touches.
    """
    prefixes = ("rename from ", "rename to ", "copy from ", "copy to ")
    paths: list[str] = []
    for line in diff.splitlines():
        for prefix in prefixes:
            if line.startswith(prefix):
                paths.append(line[len(prefix) :].strip().strip('"'))
    return paths


def _changed_line_count(diff: str) -> int:
    """Added plus deleted lines; file headers and hunk headers are not changes."""
    lines = diff.splitlines()
    headers = {index for pair in _header_pairs(lines) for index in pair}
    return sum(
        1
        for index, line in enumerate(lines)
        if index not in headers and line.startswith(("+", "-"))
    )


def _binary_marker(diff: str) -> str | None:
    """The binary marker a diff carries, if any."""
    for marker in _BINARY_MARKERS:
        if marker in diff:
            return marker
    return None


def _deleted_file(diff: str) -> str | None:
    """The path a diff deletes, or ``None`` when it deletes nothing.

    A null target side (``+++ /dev/null``) is the deletion; ``deleted file mode``
    is git's own header for the same thing and catches a diff whose headers are
    missing or malformed. A null *source* side (``--- /dev/null``) is a new file,
    which stays allowed inside an allowed glob.
    """
    for source, target in _diff_files(diff):
        if target is None:
            return source if source is not None else ""
    lines = diff.splitlines()
    if any(line.startswith("deleted file mode") for line in lines):
        named = [path for path in _diff_header_paths(diff) if path]
        return named[0] if named else ""
    return None


# ------------------------------------------------------------------
# The git check.
# ------------------------------------------------------------------


def _git_error_category(stderr: str) -> str:
    """A short category from the first stderr line; never the diff itself."""
    lines = (stderr or "").splitlines()
    first = lines[0].strip().lower() if lines else ""
    for needle, category in _GIT_ERROR_CATEGORIES:
        if needle in first:
            return category
    return "git_error"


def _git_apply_check(diff: str, repo_root: Path) -> None:
    """``git apply --check`` on ``diff`` inside ``repo_root``; nothing is written.

    The patch travels on stdin, so no temp file is created and the working tree
    is only read — this is the check-only mode the plan asks for. ``encoding`` is
    explicit: the diff is UTF-8 text, and a locale-guided default codec would
    corrupt non-ASCII context on a Chinese Windows host.
    """
    try:
        completed = subprocess.run(
            list(_GIT_COMMAND),
            cwd=str(repo_root),
            input=diff,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GIT_APPLY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise PayloadBoundaryError(
            f"git apply --check failed: timeout_after_{int(GIT_APPLY_TIMEOUT_S)}s"
        ) from None
    except OSError:
        raise PayloadBoundaryError("git apply --check failed: git_unavailable") from None
    if completed.returncode != 0:
        raise PayloadBoundaryError(
            f"git apply --check failed: {_git_error_category(completed.stderr)}"
        )


# ------------------------------------------------------------------
# The layer-1 boundary gate.
# ------------------------------------------------------------------


def _validate_path_boundary(candidate: CodePatchCandidate, paths: tuple[str, ...]) -> None:
    """Blacklist first, then the layer whitelist, for every path in the patch."""
    layer_globs = LAYER_PATH_GLOBS[candidate.layer]
    for path in paths:
        forbidden = _forbidden_glob(path)
        if forbidden is not None:
            raise PayloadBoundaryError(f"forbidden path {path} (glob {forbidden})")
        if candidate.layer == "code":
            # The open layer: anything under rag/ that survived the blacklist.
            if not path.startswith("rag/"):
                raise PayloadBoundaryError(
                    f"{path} is outside the code layer, which allows only rag/**"
                )
        elif _matched_glob(path, layer_globs) is None:
            raise PayloadBoundaryError(
                f"{path} is outside the {candidate.layer} layer globs {list(layer_globs)}"
            )


def _validate_test_paths(test_paths: tuple[str, ...], repo_root: Path) -> None:
    """Every verification target is a real test file, and none is an exception."""
    for path in test_paths:
        forbidden = _forbidden_glob(path, tests_are_targets=True)
        if forbidden is not None:
            raise PayloadBoundaryError(f"forbidden test path {path} (glob {forbidden})")
        if not path.startswith("tests/"):
            raise PayloadBoundaryError(f"{path} must live under tests/")
        if not PurePosixPath(path).name.startswith("test_") or not path.endswith(".py"):
            raise PayloadBoundaryError(f"{path} must be a test_*.py file")
        if not (repo_root / path).is_file():
            raise PayloadBoundaryError(f"{path} does not exist under the repository")


def _validate_acl_regression_tests(
    paths: tuple[str, ...], test_paths: tuple[str, ...]
) -> None:
    """A patch that touches an ACL/audit call site must name a permission test (C-10).

    The blacklist protects the permission *modules*; it cannot protect the *call sites* inside
    ``pipeline.py`` / ``guard.py`` / ``retriever.py``, which legitimate patches must be able to
    edit. This closes that blind side without forbidding the files: the diff is refused unless the
    candidate's verification targets include a permission test derived from the file it touches.
    The sandbox runs the same tests (they are also in the fixed net), so the requirement is
    evidence, not a formality.
    """
    touched = acl_call_site_paths(paths)
    if not touched:
        return
    required = required_acl_test_paths(paths)
    if set(test_paths) & set(required):
        return
    raise PayloadBoundaryError(
        f"diff touches an ACL/audit call site ({', '.join(touched)}): test_paths must include a "
        f"permission test derived from it ({' / '.join(required)}); "
        "the permission boundary can move through these files even though the permission modules "
        "are forbidden"
    )


def _validate_diff_quotas(candidate: CodePatchCandidate, paths: tuple[str, ...]) -> None:
    """Files, changed lines, binary content, deletion and unnamed files."""
    if len(paths) > MAX_PATCH_FILES:
        raise PayloadBoundaryError(
            f"{len(paths)} files exceed the limit of {MAX_PATCH_FILES}"
        )
    changed = _changed_line_count(candidate.diff)
    if changed > MAX_PATCH_CHANGED_LINES:
        raise PayloadBoundaryError(
            f"diff changes {changed} lines, limit {MAX_PATCH_CHANGED_LINES}"
        )
    if _binary_marker(candidate.diff) is not None:
        raise PayloadBoundaryError("diff carries a binary patch")
    deleted = _deleted_file(candidate.diff)
    if deleted is not None:
        detail = f": {deleted}" if deleted else ""
        raise PayloadBoundaryError(f"diff deletes a file{detail}")
    named = set(paths)
    for touched in [*_diff_header_paths(candidate.diff), *_diff_renamed_paths(candidate.diff)]:
        if _normalize_path(touched) not in named:
            raise PayloadBoundaryError(f"diff touches {touched}, which paths does not name")


def _validate_rollback_ref(candidate: CodePatchCandidate) -> None:
    """The rollback reference names the commit the diff applies to."""
    base_commit = candidate.rollback_ref.get("base_commit")
    if not isinstance(base_commit, str) or not base_commit:
        raise PayloadBoundaryError("rollback_ref must carry the base_commit the diff applies to")
    if not _BASE_COMMIT_PATTERN.match(base_commit):
        raise PayloadBoundaryError(
            f"rollback_ref base_commit must be 40 hexadecimal characters, got {len(base_commit)}"
        )


def validate_patch(candidate: CodePatchCandidate, *, repo_root: str | Path) -> None:
    """Assert a code-patch candidate stays inside the path boundary.

    ``repo_root`` is the repository the diff applies to; the check is
    deterministic and re-runnable at every gate (design §9 layer 1):

    1. Shape: ``paths`` and ``test_paths`` name at least one entry each.
    2. Path boundary: every path passes the blacklist first (the reason names the
       matched glob), then its layer whitelist — the open ``code`` layer only
       requires ``rag/``.
    3. ``test_paths`` name real verification targets: under ``tests/``, named
       ``test_*.py``, present under ``repo_root``, and not a blacklist exception
       (``tests/**`` itself is what a test path points at).
    3b. **ACL/audit red line (C-10)**: a diff that touches an ACL call site
       (``rag/pipeline.py`` / ``rag/guard.py`` / ``rag/retriever.py``) must name at least one
       permission test derived from that file (:data:`ACL_CALL_SITE_TESTS`). The blacklist
       protects the permission modules; this protects the call sites a legitimate patch can
       reach.
    4. Quotas: at most five files, at most 300 changed lines, no binary patch, no
       file deletion, and no file in the diff that ``paths`` does not name.
    5. ``git apply --check`` inside ``repo_root`` (stdin, read-only, 60s); a
       failure is reported as a category, never as diff content.
    6. ``rollback_ref["base_commit"]`` is the 40-hex commit the diff applies to.

    Raises :class:`PayloadBoundaryError` for a boundary violation and returns
    ``None`` on success.
    """
    root = Path(repo_root)
    paths = tuple(_normalize_path(path) for path in candidate.paths)
    test_paths = tuple(_normalize_path(path) for path in candidate.test_paths)

    if not paths:
        raise PayloadBoundaryError("paths is empty; a patch must name at least one file")
    if not test_paths:
        raise PayloadBoundaryError(
            "test_paths is empty; a patch must name at least one verification test"
        )

    _validate_path_boundary(candidate, paths)
    _validate_test_paths(test_paths, root)
    _validate_acl_regression_tests(paths, test_paths)
    _validate_diff_quotas(candidate, paths)
    _git_apply_check(candidate.diff, root)
    _validate_rollback_ref(candidate)


__all__ = [
    "ACL_CALL_SITE_TESTS",
    "ARTIFACT_TYPE",
    "FORBIDDEN_GLOBS",
    "GIT_APPLY_TIMEOUT_S",
    "LAYERS",
    "LAYER_PATH_GLOBS",
    "MAX_PATCH_CHANGED_LINES",
    "MAX_PATCH_FILES",
    "PERMISSION_REGRESSION_TESTS",
    "CodePatchCandidate",
    "acl_call_site_paths",
    "required_acl_test_paths",
    "validate_patch",
]
