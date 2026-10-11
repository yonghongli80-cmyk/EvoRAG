"""Overlap detection: which candidates are variants of the same change.

The review queue filled up with near-duplicates — nineteen code patches across three files, five of
them the same synonym-alignment idea written five different ways — and nothing told the reviewer that
choosing two of them would leave two implementations of one idea side by side. That is how a queue of
patches becomes a pile: not from applying them, but from keeping several variants of the same change.

This module answers one question deterministically: **which function does a patch change, and which
other candidates change the same one?** It uses the AST symbol index rather than diff-text similarity,
because the unit a reviewer reasons about is the function, and because a similarity score would be a
number nobody can act on. The key is a string like ``rag/reranker.py:Reranker.rerank``, and a group of
two or more candidates sharing a key is reported as "variants of one change".

A hint, never a gate: nothing here blocks anything, and a key computed from a moved tree only makes
the hint less precise, which is why the index is cached per process rather than re-parsed per request.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from rag.evolution.symbols import SymbolIndex

#: Parsed once per process: the index feeds hints, not gates, so a tree that moved a little only
#: makes the hint less precise. Re-parsing per request would cost more than the hint is worth.
_INDEX_CACHE: dict[str, SymbolIndex] = {}


def _index(repo_root: str | Path, files: Sequence[str]) -> SymbolIndex:
    key = f"{Path(repo_root).resolve()}::{','.join(sorted(files))}"
    index = _INDEX_CACHE.get(key)
    if index is None:
        index = SymbolIndex.build(repo_root, sorted(files))
        _INDEX_CACHE[key] = index
    return index


def _hunk_starts(diff: str) -> tuple[tuple[str, int], ...]:
    """``(path, first_line)`` per hunk — what the diff claims to touch."""
    import re

    starts: list[tuple[str, int]] = []
    current = ""
    for line in str(diff or "").splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            continue
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line)
        if match and current:
            starts.append((current, int(match.group(1))))
    return tuple(starts)


def overlap_key(diff: str, *, repo_root: str | Path, files: Sequence[str]) -> str:
    """``path:Symbol`` for the first hunk that lands inside a known symbol, else ``path``.

    "First" is deliberate: a patch that touches several places is still *about* one change, and the
    first hunk is where the model put its explanation of it.
    """
    hunks = _hunk_starts(diff)
    if not hunks:
        return ""
    index = _index(repo_root, files)
    paths = {path for path, _ in hunks}
    for path, line in hunks:
        symbols = [symbol for symbol in index.symbols if symbol.path == path]
        enclosing = [
            symbol for symbol in symbols if symbol.start <= line <= symbol.end
        ]
        if enclosing:
            # 最内层的那个：范围最小的。
            inner = min(enclosing, key=lambda symbol: symbol.end - symbol.start)
            return f"{path}:{inner.qualname}"
    first_path = hunks[0][0]
    return first_path if first_path in paths else ""


def group_by_overlap(
    rows: Iterable[Mapping], *, repo_root: str | Path, files: Sequence[str]
) -> tuple[dict[int, str], dict[str, list[int]]]:
    """``(key per candidate id, ids per key)`` — ids only, and only for groups of two or more.

    Callers use this to say "these are variants of one change"; a lone candidate never gets a group.
    """
    keys: dict[int, str] = {}
    for row in rows:
        candidate_id = row.get("id")
        diff = row.get("diff")
        if candidate_id is None or not diff:
            continue
        key = overlap_key(str(diff), repo_root=repo_root, files=files)
        if key:
            keys[int(candidate_id)] = key
    groups: dict[str, list[int]] = defaultdict(list)
    for candidate_id, key in keys.items():
        groups[key].append(candidate_id)
    return keys, {key: sorted(ids) for key, ids in groups.items() if len(ids) > 1}


__all__ = ["group_by_overlap", "overlap_key"]
