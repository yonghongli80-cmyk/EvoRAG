"""
RAG 系统评估脚本
================
加载评估数据集，对每个问题调用 RAG API，计算 Hit Rate。

用法：
    python scripts/run_eval.py                          # 默认 localhost:8080
    python scripts/run_eval.py --host http://39.105.89.99:8000
    python scripts/run_eval.py --dataset data/eval/eval_dataset.jsonl
"""

import argparse
import json
import os
import time
from pathlib import Path

import requests


def load_dataset(path: str) -> list[dict]:
    """加载 JSONL 格式的评估数据集。"""
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def login(host: str, username: str, password: str) -> str:
    """登录获取 token。"""
    resp = requests.post(f"{host}/login", json={"username": username, "password": password})
    resp.raise_for_status()
    return resp.json()["token"]


def query(host: str, token: str, question: str) -> str:
    """调用 RAG 查询接口。"""
    resp = requests.post(
        f"{host}/query",
        json={"question": question},
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json().get("answer", "")


def check_hit(answer: str, expected_keywords: list[str]) -> bool:
    """Keyword-presence metric: any expected term in the answer is a hit.

    This is not semantic correctness and does not interpret negation; evaluation
    datasets should therefore use unambiguous expected terms.
    """
    answer_lower = answer.lower()
    return any(kw.lower() in answer_lower for kw in expected_keywords)


def run_eval(host: str, dataset_path: str, username: str, password: str):
    """运行评估。"""
    print("=" * 60)
    print(" RAG 系统评估")
    print("=" * 60)
    print(f" 目标:     {host}")
    print(f" 数据集:   {dataset_path}")
    print()

    # 加载数据集
    dataset = load_dataset(dataset_path)
    print(f" 题目数量: {len(dataset)}")
    print()

    # 登录
    print(" 登录中...")
    token = login(host, username, password)
    print(" 登录成功")
    print()

    # 逐题评估
    results = []
    hits = 0
    total_latency = 0

    print(" 开始评估...")
    print("-" * 60)

    for i, item in enumerate(dataset, 1):
        question = item["question"]
        expected = item["expected_keywords"]

        # 查询
        start = time.time()
        try:
            answer = query(host, token, question)
            latency = time.time() - start
            hit = check_hit(answer, expected)
        except Exception as e:
            answer = f"ERROR: {e}"
            latency = time.time() - start
            hit = False

        if hit:
            hits += 1
        total_latency += latency

        status = "✅" if hit else "❌"
        print(f" [{i:2d}/{len(dataset)}] {status} {latency:.1f}s | {question[:50]}...")

        results.append({
            "question": question,
            "expected_keywords": expected,
            "answer": answer[:200],  # 截断
            "hit": hit,
            "latency": latency,
        })

    # 汇总
    hit_rate = hits / len(dataset) * 100
    avg_latency = total_latency / len(dataset) * 1000

    print("-" * 60)
    print()
    print("=" * 60)
    print(" 评估报告")
    print("=" * 60)
    print(f" 总题数:     {len(dataset)}")
    print(f" 命中:       {hits}")
    print(f" 未命中:     {len(dataset) - hits}")
    print(f" Hit Rate:   {hit_rate:.1f}%")
    print(f" 平均延迟:   {avg_latency:.0f} ms")
    print("-" * 60)

    # 显示未命中的题目
    missed = [r for r in results if not r["hit"]]
    if missed:
        print()
        print(" 未命中题目:")
        for r in missed:
            print(f"   ❌ {r['question'][:60]}")
            print(f"      期望: {r['expected_keywords']}")
            print(f"      回答: {r['answer'][:100]}...")
            print()

    # 保存结果
    output_path = Path(dataset_path).parent / "eval_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "host": host,
            "dataset": dataset_path,
            "total": len(dataset),
            "hits": hits,
            "hit_rate": hit_rate,
            "avg_latency_ms": avg_latency,
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f" 结果已保存到 {output_path}")
    print("=" * 60)

    return hit_rate


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG 系统评估")
    parser.add_argument("--host", default="http://localhost:8080", help="API 地址")
    parser.add_argument("--dataset", default="data/eval/eval_dataset.jsonl", help="数据集路径")
    parser.add_argument("--username", default="admin", help="登录用户名")
    parser.add_argument("--password", default=os.getenv("RAG_ADMIN_PASSWORD"), help="登录密码（或设 RAG_ADMIN_PASSWORD）")
    args = parser.parse_args()
    if not args.password:
        parser.error("请通过 --password 或 RAG_ADMIN_PASSWORD 提供管理员密码")

    run_eval(args.host, args.dataset, args.username, args.password)
