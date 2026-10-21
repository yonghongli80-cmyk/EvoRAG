"""What is serving /query/stream on 8080? Print the live canary rollout snapshot."""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from config import settings  # noqa: E402

print("candidate_enabled =", settings.evolution_candidate_enabled)
print("canary_hard_cap =", settings.evolution_candidate_canary_percent)

from rag.evolution.candidate_store import CandidateStore  # noqa: E402
from rag.evolution.rollout import CandidateRollout  # noqa: E402

store = CandidateStore(db_path=settings.evolution_db_path)
rollout = CandidateRollout()
rollout.restore(store)
print("rollouts:", rollout._rollouts if hasattr(rollout, "_rollouts") else "?")
for stage in ("preflight", "passages"):
    questions, version = rollout.provide(stage, ["c0"])
    print(f"provide({stage}) -> version={version} n_questions={len(questions)}")
    if stage == "passages":
        print("   evidence instructions:", questions["c0.contains_answer_evidence"].instructions)

from rag.jev.questions import question_set_registry  # noqa: E402

print("registry:", {t: sorted(v) for t, v in question_set_registry().items()})
