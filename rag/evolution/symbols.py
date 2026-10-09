"""AST symbol index: where a symbol lives, and which files a diagnosis is really about.

The patch generator used to rank repository files by how often the diagnosis' wording appeared in
them. That works when a diagnosis names a module in English and degenerates when it does not — a
Chinese diagnosis contributes Chinese bigrams that match no code path, so the ranking falls back
to static file order. That is how a defect in the evidence path produced three patches to
``rag/agent.py``: nothing in the pipeline knew where the failure lived or which symbol the
diagnosis was talking about.

This module supplies those facts deterministically:

* **where a symbol lives** — ``rag.retriever.Retriever._rrf_fuse``, ``Retriever._rrf_fuse`` or
  just ``_rrf_fuse`` resolves to a file and a line range by parsing the tree with :mod:`ast`,
  never by searching text;
* **which files the diagnosis mentions** — identifiers that appear in the diagnosis *and* exist
  in the index; a Chinese sentence contributes nothing, an English identifier that is not a real
  symbol contributes nothing either;
* **a window instead of a head** — a target known to live at line 700 is excerpted as a band
  around it, because a 400-line head says nothing about line 700.

The module is pure: it reads files, writes nothing, and holds no cache. One call parses the ~110
modules of a layer once, which is far below the cost of the model call it feeds.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: How many lines of context a located target gets on each side.
BULLSEYE_RADIUS = 120

#: The widest window a single target may occupy (same budget the head-excerpt used).
BULLSEYE_MAX_LINES = 400

#: Identifiers shorter than this are not looked up: ``os``, ``id`` and friends appear in prose.
_MIN_IDENTIFIER = 3

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Symbol:
    """One definition: a class, a module-level function or a method."""

    path: str
    qualname: str
    kind: str
    start: int
    end: int


@dataclass(frozen=True, eq=False)
class SymbolIndex:
    """Every definition in a set of repository files, plus the lookups built on them."""

    symbols: tuple[Symbol, ...]
    by_qualname: dict[str, list[Symbol]] = field(default_factory=dict, repr=False, compare=False)
    by_bare: dict[str, list[Symbol]] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def build(cls, repo_root: str | Path, files: Sequence[str]) -> SymbolIndex:
        """Parse ``files`` (repository-relative) into symbols, ignoring unreadable ones.

        A file that cannot be read or parsed contributes nothing rather than failing the call:
        the index is an aid to generation, never a gate on it. Syntax that needs a newer Python
        than this interpreter understands degrades exactly the same way.
        """
        root = Path(repo_root)
        symbols: list[Symbol] = []
        for path in files:
            try:
                source = (root / path).read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
            except (OSError, SyntaxError, ValueError):
                continue
            symbols.extend(_symbols_of(path, tree))
        symbols.sort(key=lambda symbol: (symbol.path, symbol.start))
        by_qualname: dict[str, list[Symbol]] = {}
        by_bare: dict[str, list[Symbol]] = {}
        for symbol in symbols:
            by_qualname.setdefault(symbol.qualname, []).append(symbol)
            by_bare.setdefault(symbol.qualname.rsplit(".", 1)[-1], []).append(symbol)
        return cls(symbols=tuple(symbols), by_qualname=by_qualname, by_bare=by_bare)

    def locate(self, name: str) -> Symbol | None:
        """Resolve ``name`` to one symbol, tolerantly and deterministically.

        Accepts a fully qualified name, a dotted suffix, a bare name, and a trailing ``()``.
        Ambiguity resolves to the first symbol in ``(path, start)`` order so the same input
        always yields the same file. A name that does not exist returns ``None``.
        """
        query = str(name or "").strip().removesuffix("()").strip()
        if not query:
            return None
        candidates = self.by_qualname.get(query)
        if candidates:
            return candidates[0]
        bare = query.rsplit(".", 1)[-1]
        candidates = self.by_bare.get(bare)
        if candidates:
            return candidates[0]
        suffix = f".{query}"
        matches = [symbol for symbol in self.symbols if symbol.qualname.endswith(suffix)]
        return matches[0] if matches else None

    def symbols_in_text(self, text: str, *, limit: int = 4) -> tuple[Symbol, ...]:
        """The real symbols the text names, in order of first appearance.

        Only identifiers that exist in the index count, and only one symbol per file: a
        diagnosis that never names a symbol yields nothing rather than noise.
        """
        seen: list[Symbol] = []
        paths: set[str] = set()
        for match in _IDENTIFIER.finditer(text or ""):
            name = match.group(0)
            if len(name) < _MIN_IDENTIFIER:
                continue
            symbol = self.locate(name)
            if symbol is None or symbol.path in paths:
                continue
            paths.add(symbol.path)
            seen.append(symbol)
            if len(seen) >= limit:
                break
        return tuple(seen)

    def files_in_text(self, text: str, *, limit: int = 4) -> tuple[str, ...]:
        """Repository files the text names by an identifier that exists in the index."""
        return tuple(symbol.path for symbol in self.symbols_in_text(text, limit=limit))

    def __len__(self) -> int:  # pragma: no cover - convenience for tests and callers
        return len(self.symbols)


def _symbols_of(path: str, tree: ast.AST) -> list[Symbol]:
    """Every class, function and method defined in one parsed module."""
    found: list[Symbol] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                found.append(
                    Symbol(path, f"{prefix}{child.name}", "class", child.lineno, child.end_lineno or child.lineno)
                )
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                found.append(
                    Symbol(
                        path,
                        f"{prefix}{child.name}",
                        "method" if prefix else "function",
                        child.lineno,
                        child.end_lineno or child.lineno,
                    )
                )
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def window_excerpt(
    text: str,
    *,
    center: int,
    radius: int = BULLSEYE_RADIUS,
    max_lines: int = BULLSEYE_MAX_LINES,
) -> tuple[str, int, int, bool]:
    """``(excerpt, first_line, last_line, truncated)``: a band of ``text`` around ``center``.

    ``center`` is 1-based. A file that fits in ``max_lines`` is returned whole and untruncated.
    Otherwise the band is ``max_lines`` lines wide at most, centred on ``center`` and clamped to
    the file, so a target known to live at line 700 is actually shown.
    """
    lines = text.splitlines()
    total = len(lines)
    if total <= max_lines:
        return text, 1, total, False
    span = min(max_lines, max(1, radius * 2 + 1))
    start = max(1, min(center, total) - radius)
    end = min(total, start + span - 1)
    start = max(1, end - span + 1)
    return "\n".join(lines[start - 1 : end]), start, end, True


__all__ = ["BULLSEYE_MAX_LINES", "BULLSEYE_RADIUS", "Symbol", "SymbolIndex", "window_excerpt"]
