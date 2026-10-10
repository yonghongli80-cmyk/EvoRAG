"""Record the four replay seams once, where credentials exist.

Layer 2 cannot run inside the sandbox because the sandbox strips credentials while this pipeline
calls the embedding service, the rerank service, the router model and the rewriter model. This
script runs the benchmark with those four seams wrapped so every answer is remembered, then writes
one fixture next to the corpus fingerprint and the seam hashes that make it verifiable later.

    python scripts/record_replay_fixture.py --kb-id kb_accessories \
        --cases benchmarks/jev_retrieval_cases.json --out real-case-handoff/evolab/replay-fixture.json

It needs the live corpus *and* working credentials, so run it where the service runs. Point
``RAG_QDRANT_PATH`` at a snapshot when the service holds the lock: the fingerprint recorded here is
computed from that same corpus, which is the one the replay will read.

The recording is only as useful as it is complete: the counts printed at the end are what the
replay will be able to answer, and a replay that asks for anything else fails loudly rather than
reaching the network (``FixtureMiss``).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402

settings.jev_mode = "active"
settings.jev_kb_allowlist = "rag_docs,kb_hr,kb_accessories,kb_after_sales,kb_evolab"


def _head_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - the commit is information, never a precondition
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-id", default="kb_accessories")
    parser.add_argument("--cases", default="benchmarks/jev_retrieval_cases.json")
    parser.add_argument("--runs", type=int, default=1, help="one pass covers every case once")
    parser.add_argument("--warmups", type=int, default=0)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    from rag.evolution.replay_fixture import RecordingSession, corpus_fingerprint

    # 语料指纹取自**这次录制实际读的那份语料**；重放侧读到同一份副本才判定为新鲜。
    from rag.retriever import Retriever

    retriever = Retriever([], collection_name=args.kb_id)
    chunks = list(retriever.chunks)
    fingerprint = corpus_fingerprint(chunks)
    print(f"corpus: {len(chunks)} chunks, fingerprint {fingerprint[:16]}", flush=True)

    from scripts.benchmark_jev_retrieval import main as benchmark_main

    started = time.perf_counter()
    with RecordingSession() as session:
        exit_code = benchmark_main(
            [
                "--kb-id", args.kb_id,
                "--cases", args.cases,
                "--runs", str(args.runs),
                "--warmups", str(args.warmups),
                "--user-id", str(args.user_id),
            ]
        )
    fixture = session.fixture(corpus=fingerprint, base_commit=_head_commit())
    # 交给 Fixture.save() 自己写：手写字段清单漏过一次新表（Jev 的两个映射），不能让格式定义有第二份。
    fixture.save(args.out)
    print(
        json.dumps(
            {
                "out": args.out,
                "benchmark_exit": exit_code,
                "elapsed_s": round(time.perf_counter() - started, 1),
                "embeddings": len(fixture.embeddings),
                "reranks": len(fixture.reranks),
                "routes": len(fixture.routes),
                "rewrites": len(fixture.rewrites),
                "jev_preflights": len(fixture.jev_preflights),
                "jev_passages": len(fixture.jev_passages),
                "base_commit": fixture.base_commit[:8],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
