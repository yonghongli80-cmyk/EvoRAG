"""近重复提示：哪些候选其实是"同一条改动的不同写法"。

动机（实测）：19 条代码补丁挤在 3 个文件，其中 5 条是同一个"同义对齐"想法的变体；队列本身不告诉
评审"选两条会并排留下两套实现"。这里只做**提示**，从不拦截。
"""

from __future__ import annotations

from pathlib import Path

from rag.evolution.overlap import group_by_overlap, overlap_key

ROOT = Path(__file__).resolve().parents[1]
FILES = ("rag/reranker.py", "rag/pipeline.py")


def _diff(path: str, line: int, added: str = "+    # tweak") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -{line},3 +{line},4 @@\n"
        " def rerank(query, documents):\n"
        f"{added}\n"
    )


def test_the_key_names_the_function_a_patch_changes():
    """键是"哪个文件的哪个函数"，不是相似度分数——因为评审要判断的单位是函数。"""
    from rag.evolution.symbols import SymbolIndex

    index = SymbolIndex.build(ROOT, list(FILES))
    symbol = index.locate("rerank") or index.locate("Reranker.rerank")
    assert symbol is not None, "夹具依赖真实仓库里的 rerank 函数"

    key = overlap_key(_diff(symbol.path, symbol.start + 1), repo_root=ROOT, files=FILES)

    assert key.startswith(f"{symbol.path}:")
    assert symbol.qualname.split(".")[-1] in key


def test_variants_of_one_change_are_grouped_and_a_lone_patch_is_not():
    from rag.evolution.symbols import SymbolIndex

    index = SymbolIndex.build(ROOT, list(FILES))
    symbol = index.locate("rerank") or index.locate("Reranker.rerank")
    assert symbol is not None
    same = symbol.start + 1
    rows = [
        {"id": 1, "diff": _diff(symbol.path, same, "+    # variant A")},
        {"id": 2, "diff": _diff(symbol.path, same, "+    # variant B")},
        {"id": 3, "diff": ""},
    ]

    keys, groups = group_by_overlap(rows, repo_root=ROOT, files=FILES)

    assert keys[1] == keys[2]
    assert groups[keys[1]] == [1, 2]
    assert 3 not in keys  # 没有 diff 就没有键（也就不参与任何"同类"提示）
    assert all(len(ids) > 1 for ids in groups.values())
