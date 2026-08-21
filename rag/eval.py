"""评估模块 — 自动化 RAG 系统质量评估"""
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime


@dataclass
class EvalResult:
    question: str
    expected_keywords: list[str]
    answer: str
    hit: bool
    sources: list[dict]
    latency_ms: float


def load_dataset(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def evaluate(pipeline, dataset: list[dict]) -> list[EvalResult]:
    results = []
    for item in dataset:
        start = time.time()
        result = pipeline.query(item["question"])
        latency = (time.time() - start) * 1000
        keywords = item["expected_keywords"]
        hit = any(kw.lower() in result.answer.lower() for kw in keywords)
        results.append(EvalResult(
            question=item["question"],
            expected_keywords=keywords,
            answer=result.answer,
            hit=hit,
            sources=result.sources,
            latency_ms=latency,
        ))
    return results


def compute_metrics(results: list[EvalResult]) -> dict:
    total = len(results)
    hits = sum(1 for r in results if r.hit)
    latencies = sorted(r.latency_ms for r in results)
    avg_latency = sum(latencies) / total if total else 0
    p95_idx = int(total * 0.95)
    p95 = latencies[min(p95_idx, total - 1)] if latencies else 0
    return {
        "total": total,
        "hit_rate": hits / total if total else 0,
        "avg_latency_ms": round(avg_latency, 1),
        "p95_latency_ms": round(p95, 1),
        "pass": hits,
        "fail": total - hits,
    }


def save_bad_case(result: EvalResult, path: str = "data/bad_cases.jsonl") -> None:
    """失败用例自动归档。"""
    entry = {
        "question": result.question,
        "expected_keywords": result.expected_keywords,
        "actual_answer": result.answer[:200],
        "hit": result.hit,
        "latency_ms": result.latency_ms,
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def print_report(results: list[EvalResult], metrics: dict) -> None:
    print(f"\n{'='*60}")
    print(f"评估报告: {metrics['pass']}/{metrics['total']} 通过 "
          f"(Hit Rate: {metrics['hit_rate']:.1%})")
    print(f"平均延迟: {metrics['avg_latency_ms']:.0f}ms")
    print(f"P95 延迟: {metrics['p95_latency_ms']:.0f}ms")
    print(f"{'='*60}")
    for i, r in enumerate(results, 1):
        status = "✅" if r.hit else "❌"
        print(f"{status} Q{i}: {r.question}")
        if not r.hit:
            print(f"   期望关键词: {r.expected_keywords}")
            print(f"   实际回答: {r.answer[:100]}...")
    print()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="RAG 评估系统")
    parser.add_argument("--dataset", required=True, help="评估数据集路径 (JSONL)")
    parser.add_argument("--file", required=True, help="要索引的文档路径")
    parser.add_argument("--history", default="data/eval/eval_history.jsonl", help="评估历史记录路径")
    args = parser.parse_args()

    from rag.pipeline import RAGPipeline

    dataset = load_dataset(args.dataset)
    if not dataset:
        print("数据集为空，请检查文件路径")
        sys.exit(1)

    print(f"正在索引文档: {args.file}")
    pipeline = RAGPipeline(args.file)

    print(f"开始评估 ({len(dataset)} 个问题)...")
    results = evaluate(pipeline, dataset)
    metrics = compute_metrics(results)
    print_report(results, metrics)

    for r in results:
        if not r.hit:
            save_bad_case(r)

    history_entry = {
        "timestamp": datetime.now().isoformat(),
        "dataset": args.dataset,
        "file": args.file,
        **metrics,
    }
    os.makedirs(os.path.dirname(args.history) or ".", exist_ok=True)
    with open(args.history, "a", encoding="utf-8") as f:
        f.write(json.dumps(history_entry, ensure_ascii=False) + "\n")
    print(f"评估结果已追加到: {args.history}")


if __name__ == "__main__":
    main()
