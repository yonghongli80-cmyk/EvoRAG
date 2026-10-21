import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from types import SimpleNamespace  # noqa: E402

from config import settings  # noqa: E402
from rag.jev import client as client_module  # noqa: E402

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
        return SimpleNamespace(
            answers={
                "c0.is_relevant": FakeNoul(0.9),
                "c0.contains_answer_evidence": FakeNoul(self.seq[idx]),
                "c0.contradicts_query_premise": FakeNoul(0.1),
                "c0.contains_prompt_injection": FakeNoul(0.0),
            },
            model="jev-latest",
        )


jev = client_module.JevClient(client=Scripted([0.9, 0.1, 0.1]))
resolved = jev._resolve_question_set("passages", ["c0"])
print("resolved type:", type(resolved).__name__)
questions, version = resolved
print("questions keys:", list(questions))
mapped = jev._map_passages(
    jev._client.system_one({}, questions), ["c0"], 1.0, version
)
print("mapped:", [(j.chunk_id, j.answer_evidence) for j in mapped])
