"""补丁生成成功率基线：固定真实 (聚类, 观测) 集合 × M 次真实生成。

**为什么需要它（判据先行）**：队列 4/6 之后残余的失败是**模型输出质量**问题——实测约每 4 次生成就有
1~3 次被边界拒绝（`does_not_apply` / `corrupt_patch` / 编造不存在的测试文件名）。任何"加大上下文 /
逐文件结构化输出 / 两阶段生成"的改动都需要一条**可比较的基线**，否则无法区分"改进"与"噪声"。

**判据（本项目既定）**：在已确认 `code` / `reranking` 层级的真实聚类上，**生成成功率 ≥ 80%**
（成功率 = 过了边界：`git apply --check` 通过且 `test_paths` 真实存在）。

**只读**：不写候选、不写观测（`PatchGenerator.generate` 只从 store 读异常帧用于定位）。
**不需要语料**：只用仓库与模型 ⇒ 不与运行中的服务抢 Qdrant 本地锁。

用法::

    python scripts/measure_patch_generation.py --label patch-v4-baseline --limit 6 --repeats 2 \
        --json-out real-case-handoff/evolab/patch-gen-baseline.json \
        --markdown-out real-case-handoff/evolab/patch-gen-baseline.md
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)


def diff_shape(text: str) -> dict:
    """关于一份 diff 的**结构事实**（内容无关）：文件头/hunk 数、声明行数与实际 body 是否相符。"""
    hunks = list(_HUNK.finditer(text))
    mismatched = 0
    for index, match in enumerate(hunks):
        body_start = text.index("\n", match.end()) + 1 if "\n" in text[match.end():] else len(text)
        body_end = hunks[index + 1].start() if index + 1 < len(hunks) else len(text)
        body = text[body_start:body_end].splitlines()
        adds = sum(1 for line in body if line.startswith("+") and not line.startswith("+++"))
        dels = sum(1 for line in body if line.startswith("-") and not line.startswith("---"))
        ctx = sum(1 for line in body if line.startswith(" "))
        if (ctx + dels != int(match.group(2) or 1)) or (ctx + adds != int(match.group(4) or 1)):
            mismatched += 1
    return {
        "chars": len(text),
        "file_headers": sum(1 for line in text.splitlines() if line.startswith("diff --git ")),
        "hunks": len(hunks),
        "hunks_with_mismatched_counts": mismatched,
        "has_code_fence": "```" in text,
        "ends_with_newline": text.endswith("\n"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=6, help="取多少个 (聚类, 观测) 对")
    parser.add_argument("--repeats", type=int, default=2, help="每个对生成几次")
    parser.add_argument("--cluster-id", type=int, default=0, help="只测某个聚类（0 = 全部）")
    parser.add_argument(
        "--observation-id", type=int, default=0, help="只测某个观测（0 = 全部），用于定点复测"
    )
    parser.add_argument(
        "--observation-ids",
        default="",
        help="固定样本：逗号分隔的观测 id 列表。**做 A/B 对照时必须用它**——否则 top-up 可能选中"
        "另一批样本，两轮就不可比（我第一版就踩了这个坑）",
    )
    parser.add_argument(
        "--top-up",
        action="store_true",
        help="合格项不足时，从「已确认层级」的观测里补足样本（基线测的是**生成质量**，"
        "不是调度；worker 的准入还要求「该观测尚无候选」，那是调度条件，不该限制测量）",
    )
    parser.add_argument("--label", default="", help="给这次测量起名（写进报告）")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--markdown-out", default="")
    args = parser.parse_args(argv)

    from config import settings
    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.patch_generator import (
        PATCH_PROMPT_VERSION,
        PatchGenerator,
        _failure_category,
    )
    from rag.evolution.store import FailureTraceStore
    from rag.evolution.worker import _eligible_patch_items

    store = FailureTraceStore(settings.evolution_db_path)
    candidates = CandidateStore(store)
    items = _eligible_patch_items(store, candidates)
    if args.cluster_id:
        items = [item for item in items if int(item["cluster"]["id"]) == args.cluster_id]
    if args.top_up and len(items) < args.limit:
        # 补足：同一套"已确认层级 + 最新草稿层一致"的判据，但**不**要求尚无候选
        # （那是 worker 的调度条件，与生成质量无关）。按聚类轮转取样，避免全落在同一聚类。
        newest: dict[int, dict] = {}
        for draft in store.list_drafts():
            if draft.get("observation_id") is not None:
                newest.setdefault(int(draft["observation_id"]), draft)
        have = {(int(item["cluster"]["id"]), int(item["observation"]["id"])) for item in items}
        extra: dict[int, list[dict]] = {}
        for cluster in store.list_clusters():
            layer = str(cluster.get("confirmed_layer") or "")
            if cluster.get("status") != "approved_for_candidate" or not layer:
                continue
            for observation in store.list_observations(cluster_id=cluster["id"]):
                draft = newest.get(int(observation["id"]))
                if draft is None or (draft.get("payload") or {}).get("suggested_layer") != layer:
                    continue
                if (int(cluster["id"]), int(observation["id"])) in have:
                    continue
                extra.setdefault(int(cluster["id"]), []).append(
                    {"cluster": cluster, "observation": observation, "draft": draft}
                )
        while len(items) < args.limit and any(extra.values()):
            for cluster_id in sorted(extra):
                if len(items) >= args.limit:
                    break
                if extra[cluster_id]:
                    items.append(extra[cluster_id].pop(0))
    if args.observation_ids:
        wanted = [int(x) for x in str(args.observation_ids).split(",") if x.strip()]
        fixed: list[dict] = []
        for cluster in store.list_clusters():
            for observation in store.list_observations(cluster_id=cluster["id"]):
                if int(observation["id"]) not in wanted:
                    continue
                draft = next(
                    (
                        row
                        for row in store.list_drafts()
                        if int(row.get("observation_id") or 0) == int(observation["id"])
                    ),
                    None,
                )
                fixed.append(
                    {"cluster": cluster, "observation": observation, "draft": draft or {"payload": {}}}
                )
        order = {value: index for index, value in enumerate(wanted)}
        fixed.sort(key=lambda item: order.get(int(item["observation"]["id"]), 999))
        items = fixed
    if args.observation_id:
        # 定点复测：绕过"尚无候选"的调度条件，直接在合格层级上复测该观测。
        target = next(
            (
                observation
                for cluster in store.list_clusters()
                if str(cluster.get("confirmed_layer") or "")
                for observation in store.list_observations(cluster_id=cluster["id"])
                if int(observation["id"]) == args.observation_id
            ),
            None,
        )
        if target is None:
            print(json.dumps({"error": f"observation {args.observation_id} not found"}, ensure_ascii=False))
            return 1
        draft = next(
            (
                row
                for row in store.list_drafts()
                if int(row.get("observation_id") or 0) == args.observation_id
            ),
            None,
        )
        cluster = next(
            c for c in store.list_clusters() if int(c["id"]) == int(target["cluster_id"])
        )
        items = [{"cluster": cluster, "observation": target, "draft": draft or {"payload": {}}}]
    items = items[: args.limit]
    if not items:
        print(json.dumps({"error": "no eligible (cluster, observation) pairs"}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "label": args.label,
                "prompt_version": PATCH_PROMPT_VERSION,
                "timeout_seconds": float(
                    getattr(settings, "evolution_patch_timeout_seconds", 600.0)
                ),
                "pairs": [
                    {
                        "cluster": int(item["cluster"]["id"]),
                        "observation": int(item["observation"]["id"]),
                        "layer": item["cluster"].get("confirmed_layer"),
                    }
                    for item in items
                ],
                "repeats": args.repeats,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    rows: list[dict] = []
    for pair_index, item in enumerate(items, start=1):
        cluster = item["cluster"]
        observation = item["observation"]
        layer = str(cluster.get("confirmed_layer") or "")
        draft = item["draft"].get("payload") or {}
        for repeat in range(1, args.repeats + 1):
            captured: dict = {}

            def model_call(messages, _captured=captured):
                completion = PatchGenerator(
                    timeout_seconds=float(
                        getattr(settings, "evolution_patch_timeout_seconds", 600.0)
                    )
                )._model_call(messages)
                _captured["completion"] = completion or ""
                return completion

            generator = PatchGenerator(
                model_call=model_call,
                # 与 worker 同源：用配置里的超时（实测单次 42–350s，默认 90s 会截断慢调用）。
                timeout_seconds=float(getattr(settings, "evolution_patch_timeout_seconds", 600.0)),
            )
            started = time.perf_counter()
            entry = {
                "cluster": int(cluster["id"]),
                "observation": int(observation["id"]),
                "layer": layer,
                "repeat": repeat,
                "prompt_version": PATCH_PROMPT_VERSION,
            }
            try:
                candidate = generator.generate(
                    cluster,
                    [draft],
                    layer=layer,
                    repo_root=ROOT,
                    observation_id=int(observation["id"]),
                    cluster_id=int(cluster["id"]),
                    trace_store=store,
                )
                entry["result"] = "valid"
                entry["paths"] = list(candidate.paths)
                entry["shape"] = diff_shape(candidate.diff)
                entry["test_paths"] = list(candidate.test_paths)
            except Exception as error:  # noqa: BLE001 - 失败本身就是被测对象
                reason = getattr(error, "reason", "")
                detail = getattr(error, "detail", "") or str(error)
                entry["result"] = "refused"
                entry["category"] = _failure_category(reason, detail)
                entry["detail"] = f"{reason}: {detail}"[:160]
                completion = captured.get("completion") or ""
                entry["completion"] = {
                    "chars": len(completion),
                    "has_diff_git": "diff --git" in completion,
                    "has_hunk_header": "@@" in completion,
                    "new_file_headers": sum(
                        1 for line in completion.splitlines() if line.startswith("+++ b/")
                    ),
                }
            entry["model_calls"] = int(getattr(generator, "model_calls", 0))
            entry["elapsed_s"] = round(time.perf_counter() - started, 1)
            rows.append(entry)
            print(
                f"[{pair_index}/{len(items)} · {repeat}/{args.repeats}] "
                f"cluster={entry['cluster']} obs={entry['observation']} → {entry['result']}"
                f"{'/' + entry['category'] if entry.get('category') else ''} "
                f"({entry['model_calls']} calls, {entry['elapsed_s']}s)",
                flush=True,
            )
    store.close()

    attempts = len(rows)
    valid = [row for row in rows if row["result"] == "valid"]
    first_try = [row for row in valid if row["model_calls"] == 1]
    by_category = Counter(row.get("category", "") for row in rows if row["result"] == "refused")
    per_cluster: dict[str, dict] = defaultdict(lambda: {"attempts": 0, "valid": 0})
    for row in rows:
        bucket = per_cluster[str(row["cluster"])]
        bucket["attempts"] += 1
        bucket["valid"] += 1 if row["result"] == "valid" else 0
    summary = {
        "label": args.label,
        "prompt_version": PATCH_PROMPT_VERSION,
        "pairs": len(items),
        "repeats": args.repeats,
        "attempts": attempts,
        "valid": len(valid),
        "valid_first_try": len(first_try),
        "success_rate": round(len(valid) / attempts, 4) if attempts else 0.0,
        "first_try_rate": round(len(first_try) / attempts, 4) if attempts else 0.0,
        "by_category": dict(by_category),
        "mean_model_calls": round(statistics.fmean(row["model_calls"] for row in rows), 2),
        "per_cluster": dict(per_cluster),
        "criterion": "generation success >= 80% on confirmed code/reranking clusters",
        "criterion_met": (len(valid) / attempts) >= 0.8 if attempts else False,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.markdown_out:
        lines = [
            f"# 补丁生成成功率基线（{args.label or '未命名'}，{PATCH_PROMPT_VERSION}）",
            "",
            f"* 判据：**{summary['criterion']}**",
            f"* 结果：**{summary['valid']}/{summary['attempts']} = {summary['success_rate']:.1%}**"
            f"（首试 {summary['valid_first_try']}/{summary['attempts']} = {summary['first_try_rate']:.1%}）"
            f" ⇒ **{'达标' if summary['criterion_met'] else '未达标'}**",
            f"* 平均模型调用：{summary['mean_model_calls']} 次/次生成",
            f"* 失败类别分布：`{json.dumps(summary['by_category'], ensure_ascii=False)}`",
            "",
            "| 聚类 | 观测 | 层 | 第几次 | 结果 | 类别 | 调用 | 耗时(s) |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in rows:
            lines.append(
                f"| {row['cluster']} | {row['observation']} | {row['layer']} | {row['repeat']} | "
                f"{row['result']} | {row.get('category', '—')} | {row['model_calls']} | "
                f"{row['elapsed_s']} |"
            )
        Path(args.markdown_out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
