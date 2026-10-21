"""Which labeled case answers where a refusal was required, on the current production path?"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from scripts.benchmark_jev_retrieval import build_live_preparations, load_cases  # noqa: E402

LABELED = ("real-012", "real-017", "real-018", "real-020")
cases = {c.id: c for c in load_cases("benchmarks/real-cases-v1.json") if c.id in LABELED}

old, new = build_live_preparations(kb_id="rag_docs", user_id=1)
for attempt in range(1, 4):
    print(f"--- attempt {attempt}")
    for case_id in LABELED:
        outcome = new(cases[case_id], session_id=f"diag-{attempt}-{case_id}")
        print(
            f"  {case_id}: expected={cases[case_id].expected_position} "
            f"route={outcome.route} answerability={outcome.answerability} "
            f"sources={len(outcome.source_names)}"
        )
