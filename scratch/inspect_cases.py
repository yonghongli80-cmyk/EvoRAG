import json
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

with open("benchmarks/jev_retrieval_cases.json", encoding="utf-8") as handle:
    data = json.load(handle)
print("top-level type:", type(data).__name__)
if isinstance(data, dict):
    print("keys:", list(data))
    cases = data.get("cases", data)
else:
    cases = data
print("n:", len(cases))
print("kb distribution:", Counter(c.get("kb_id") for c in cases))
print("route distribution:", Counter(c.get("expected_route") for c in cases))
for c in cases:
    print(f"  {c.get('id')} [{c.get('expected_route')}] {c.get('kb_id')}: {c.get('question')}")
