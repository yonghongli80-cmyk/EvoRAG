import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from types import SimpleNamespace  # noqa: E402

from config import settings  # noqa: E402
from rag.jev import client as client_module  # noqa: E402

settings.jev_vote_rounds = 3

original_run = client_module.JevClient._run


def traced_run(self, stage, state, questions, mapper):
    result = original_run(self, stage, state, questions, mapper)
    first = None
    if result.value:
        first = [getattr(j, "answer_evidence", None) for j in result.value][:3]
    print(
        f"   _run stage={stage} available={result.available} kind={result.failure_kind} "
        f"value_sample={first} samples={getattr(self, '_samples', 'NOATTR')}"
    )
    return result


client_module.JevClient._run = traced_run


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


c = Scripted([0.9, 0.1, 0.1])
print("sequence [0.9, 0.1, 0.1]")
r = client_module.JevClient(client=c).judge_passages(
    {"question": "q", "passages": {"c0": {"text": "t"}}}, ["c0"]
)
print(f" -> calls={c.calls} kind={r.failure_kind}")
