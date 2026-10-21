import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from types import SimpleNamespace  # noqa: E402

from config import settings  # noqa: E402
from rag.jev.client import JevClient  # noqa: E402

settings.jev_vote_rounds = 3


class FakeNoul:
    def __init__(self, noul):
        self.noul = noul


class Scripted:
    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0

    def system_one(self, state, questions, model=None):
        self.calls += 1
        idx = min(self.calls - 1, len(self.seq) - 1)
        v = self.seq[idx]
        print(f"   [call {self.calls}] evidence={v}")
        return SimpleNamespace(
            answers={
                "c0.is_relevant": FakeNoul(0.9),
                "c0.contains_answer_evidence": FakeNoul(v),
                "c0.contradicts_query_premise": FakeNoul(0.1),
                "c0.contains_prompt_injection": FakeNoul(0.0),
            },
            model="jev-latest",
        )


for seq in ([0.9, 0.1, 0.1], [0.9, 0.1, 0.9]):
    c = Scripted(seq)
    print("sequence", seq)
    r = JevClient(client=c).judge_passages({"question": "q", "passages": {"c0": {"text": "t"}}}, ["c0"])
    print(f" -> calls={c.calls} available={r.available} kind={r.failure_kind}\n")
