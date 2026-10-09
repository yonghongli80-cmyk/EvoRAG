"""The AST symbol index: where a symbol lives, and which files a diagnosis names.

Motivation, measured: a Chinese diagnosis contributes Chinese bigrams that match no code path,
so the generator's keyword ranking degenerated to static file order, and cluster #30's defect in
the evidence path produced three patches to ``rag/agent.py``. The index replaces that guesswork
with parsing: only identifiers that really exist can steer a patch.
"""

from __future__ import annotations

from pathlib import Path

from rag.evolution.symbols import (
    BULLSEYE_MAX_LINES,
    SymbolIndex,
    window_excerpt,
)

SOURCE = '''"""Module docstring."""


def top_level(alpha):
    return alpha


class Widget:
    def method_one(self):
        return 1

    async def method_two(self):
        return 2

    class Inner:
        def deep(self):
            return 3
'''


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "rag" / "pkg").mkdir(parents=True)
    (tmp_path / "rag" / "pkg" / "mod.py").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "rag" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    return tmp_path


def test_the_index_finds_definitions_and_their_line_ranges(tmp_path):
    root = _repo(tmp_path)

    index = SymbolIndex.build(root, ["rag/pkg/mod.py"])

    qualified = {symbol.qualname for symbol in index.symbols}
    assert {"top_level", "Widget", "Widget.method_one", "Widget.method_two",
            "Widget.Inner", "Widget.Inner.deep"} <= qualified
    widget = index.locate("Widget")
    assert widget is not None and widget.kind == "class"
    assert widget.start < widget.end


def test_locate_accepts_qualified_suffix_and_bare_names(tmp_path):
    index = SymbolIndex.build(_repo(tmp_path), ["rag/pkg/mod.py"])

    for query in ("Widget.method_one", "method_one", "Widget.method_one()"):
        symbol = index.locate(query)
        assert symbol is not None, query
        assert symbol.qualname == "Widget.method_one"
    assert index.locate("no_such_symbol") is None
    assert index.locate("") is None


def test_an_unparsable_file_contributes_nothing_instead_of_failing(tmp_path):
    """索引是生成的辅助，不是它的门禁：坏文件跳过，好文件照常。"""
    index = SymbolIndex.build(_repo(tmp_path), ["rag/broken.py", "rag/pkg/mod.py"])

    assert index.locate("top_level") is not None
    assert all(symbol.path != "rag/broken.py" for symbol in index.symbols)


def test_only_real_symbols_in_the_text_steer_a_patch(tmp_path):
    """跨语言断层的修法：中文句子里出现**真实存在**的标识符才生效，虚构的不产生噪声。"""
    index = SymbolIndex.build(_repo(tmp_path), ["rag/pkg/mod.py"])

    files = index.files_in_text("排序这一步把 method_one 与 method_two 都牵扯进来了，zzz_missing 不是符号")

    assert files == ("rag/pkg/mod.py",)
    assert index.files_in_text("整句话都是中文，没有任何英文标识符") == ()


def test_files_in_text_respects_its_limit(tmp_path):
    index = SymbolIndex.build(_repo(tmp_path), ["rag/pkg/mod.py", "rag/broken.py"])

    assert index.files_in_text("method_one method_two top_level Widget", limit=1) == ("rag/pkg/mod.py",)


def test_the_window_shows_the_band_around_the_centre(tmp_path):
    """靶心窗口：目标在第 700 行时，"前 400 行"什么也看不到。"""
    text = "\n".join(f"line {index}" for index in range(1, 1001))

    excerpt, first, last, truncated = window_excerpt(text, center=700)

    assert truncated is True
    assert first <= 700 <= last
    assert last - first + 1 <= BULLSEYE_MAX_LINES
    assert "line 700" in excerpt


def test_a_short_file_is_returned_whole_and_untruncated():
    text = "\n".join(f"line {index}" for index in range(1, 51))

    excerpt, first, last, truncated = window_excerpt(text, center=40)

    assert truncated is False
    assert (first, last) == (1, 50)
    assert excerpt == text


def test_a_centre_past_the_end_is_clamped():
    text = "\n".join(f"line {index}" for index in range(1, 1001))

    _excerpt, first, last, truncated = window_excerpt(text, center=10_000)

    assert truncated is True
    assert last == 1000
    assert first <= last
