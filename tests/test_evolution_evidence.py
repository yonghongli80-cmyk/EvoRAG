"""Task 2: deterministic-first evidence assembly with a reproduction check.

Phase 4 revises the granularity (``docs/evolution-diagnosis.md`` §5): the pack
now covers one member **observation**, not the whole cluster, because a cluster
key (kb × stage × root-cause feature) mixes distinct root causes and a
cluster-level draft can never match a mixed label set. ``assemble_evidence``
therefore takes ``(observation, cluster, ...)``; the pack carries that single
observation's question, its own failed chunk hashes, its stage/root-cause
feature (falling back to the cluster only when the observation left them empty),
and re-retrieves exactly once for the deterministic reproduction check.

The pack feeds the Task 3 diagnoser, so every test here pins one seam of the
interface: admin-confirmed labels first, same-KB success traces second,
re-retrieved hashes and the deterministic reproduction verdict last. No test
touches the network, a real pipeline, or any credential.
"""

import hashlib
import json
import time
from dataclasses import dataclass

from rag.evolution.evidence import assemble_evidence
from rag.evolution.models import EvidencePack, FailureObservation, FailureStage
from rag.evolution.store import FailureTraceStore


def md5(text: str) -> str:
    """Chunk hashes are the pipeline's md5 of the chunk text (rag/pipeline.py)."""

    return hashlib.md5(text.encode("utf-8")).hexdigest()


@dataclass
class Chunk:
    text: str
    doc_name: str = "doc.md"


class FakeRetriever:
    """Read-only retriever stub: a fixed chunk list per question, every call recorded."""

    def __init__(self, by_question: dict[str, tuple]):
        self._by_question = {question: list(chunks) for question, chunks in by_question.items()}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, question: str, *, kb_id: str) -> list:
        self.calls.append((question, kb_id))
        return list(self._by_question.get(question, ()))


class FakeTracker:
    """Tracker stub mirroring ExecutionTracker.get_recent: newest-first row dicts."""

    def __init__(self, rows: list[dict]):
        self._rows = list(rows)
        self.requested_limit: int | None = None

    def get_recent(self, limit: int = 20) -> list[dict]:
        self.requested_limit = limit
        return [dict(row) for row in self._rows[:limit]]


def build_cluster(
    store: FailureTraceStore,
    *,
    kb_id: str = "kb1",
    failure_stage: str = "retrieval",
    root_cause_feature: str = "recall_miss",
) -> dict:
    """Insert one cluster row straight through ``upsert_cluster`` (no members)."""
    return store.upsert_cluster(
        cluster_key=f"{kb_id}|{failure_stage}|{root_cause_feature}",
        kb_id=kb_id,
        failure_stage=failure_stage,
        root_cause_feature=root_cause_feature,
        seen_at=time.time(),
    )


def add_member(
    store: FailureTraceStore,
    cluster: dict,
    *,
    question: str,
    hashes: tuple[str, ...] = (),
    kb_id: str = "kb1",
    failure_stage: str = "retrieval",
    root_cause_feature: str | None = None,
) -> dict:
    """Add one observation, attach it to ``cluster``, and return the stored row.

    The returned row is read back after ``attach_cluster_member`` so its
    ``stage``/``root_cause_feature`` reflect the authoritative backfill the
    clusterer writes — exactly what the worker would assemble a draft from.
    """
    now = time.time()
    member = store.add_observation(
        FailureObservation(
            kb_id=kb_id,
            failure_stage=FailureStage(failure_stage),
            question=question,
            route="rag",
            chunk_hashes=hashes,
            best_score=0.05,
            answerability_kind="insufficient_evidence",
        )
    )
    store.attach_cluster_member(
        member["id"],
        cluster["id"],
        failure_stage=failure_stage,
        root_cause_feature=(
            root_cause_feature if root_cause_feature is not None else cluster["root_cause_feature"]
        ),
        now=now,
    )
    return store.get_observation(member["id"])


def write_labels(path, entries: list[dict]) -> None:
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def test_admin_labels_enter_the_pack_with_priority(tmp_path):
    """设计第 6 节：管理员确认标签是最优先级证据，进 pack 且标注优先级。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="年假额度 剩几天")
    labels_path = tmp_path / "labels.json"
    write_labels(labels_path, [
        {"case_id": "real-001", "question": "年假额度剩几天", "expected_route": "rag",
         "expected_source_names": ["假期制度.md"], "expected_answer": "每年 10 天"},
        {"case_id": "real-002", "question": "无关问题", "expected_route": "rag",
         "expected_source_names": [], "expected_answer": ""},
    ])

    pack = assemble_evidence(member, cluster, store=store, labels_path=labels_path)

    # Only the label whose normalized question matches this observation enters the pack.
    assert [label["case_id"] for label in pack.admin_labels] == ["real-001"]
    label = pack.admin_labels[0]
    assert label["priority"] == "admin_confirmed"
    assert label["expected_route"] == "rag"
    assert label["expected_source_names"] == ["假期制度.md"]


def test_label_matching_normalizes_case_and_whitespace(tmp_path):
    """问题文本规范化：strip + 去全部空白 + 小写化后才比较。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="Qdrant  故障 Fu")
    labels_path = tmp_path / "labels.json"
    write_labels(labels_path, [
        {"case_id": "real-009", "question": " qdrant 故障fu ", "expected_route": "rag"},
    ])

    pack = assemble_evidence(member, cluster, store=store, labels_path=labels_path)

    assert [label["case_id"] for label in pack.admin_labels] == ["real-009"]


def test_admin_labels_empty_without_a_labels_file(tmp_path):
    """缺省 / 文件不存在 / 空列表 → admin_labels 为空 tuple，不报错。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="q")

    assert assemble_evidence(member, cluster, store=store).admin_labels == ()
    assert assemble_evidence(
        member, cluster, store=store, labels_path=tmp_path / "missing.json"
    ).admin_labels == ()
    empty_path = tmp_path / "empty.json"
    empty_path.write_text("[]", encoding="utf-8")
    assert assemble_evidence(member, cluster, store=store, labels_path=empty_path).admin_labels == ()


def test_pack_carries_the_single_observation_failed_hashes(tmp_path):
    """Phase-4 granularity: failed_hashes are scoped to this observation only.

    A cluster gathers distinct root causes under one key; the draft must reason
    from the one trajectory it is responsible for, so a sibling member's offered
    hashes never leak into this pack.
    """

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    this_member = add_member(store, cluster, question="q1", hashes=(md5("a1"), md5("a2")))
    add_member(store, cluster, question="q2", hashes=(md5("b1"),))  # a distinct root cause

    pack = assemble_evidence(this_member, cluster, store=store)

    assert isinstance(pack, EvidencePack)
    assert pack.cluster_id == cluster["id"]
    assert pack.kb_id == "kb1"
    assert pack.failure_stage == "retrieval"
    assert pack.root_cause_feature == "recall_miss"
    assert pack.question_samples == ("q1",)  # exactly one question per member draft
    assert pack.failed_hashes == tuple(sorted((md5("a1"), md5("a2"))))  # not the sibling's b1


def test_pack_prefers_the_observation_signals_over_the_cluster(tmp_path):
    """stage / root_cause_feature come from the observation row when present."""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store, failure_stage="retrieval", root_cause_feature="recall_miss")
    member = add_member(
        store, cluster, question="q", failure_stage="generation", root_cause_feature="code_defect",
    )

    pack = assemble_evidence(member, cluster, store=store)

    assert pack.failure_stage == "generation"
    assert pack.root_cause_feature == "code_defect"


def test_pack_falls_back_to_cluster_signals_when_observation_is_empty(tmp_path):
    """Empty observation stage/feature fall back to the cluster's authoritative values."""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store, failure_stage="retrieval", root_cause_feature="recall_miss")
    # A hand-built observation row that left stage/feature empty (the cluster's
    # backfill is the only source) — assemble reads rows, so a dict suffices.
    observation = {
        "id": 999,
        "cluster_id": cluster["id"],
        "kb_id": "kb1",
        "stage": "",
        "root_cause_feature": "",
        "question": "q",
        "details_chunk_hashes": [],
    }

    pack = assemble_evidence(observation, cluster, store=store)

    assert pack.failure_stage == "retrieval"
    assert pack.root_cause_feature == "recall_miss"


def test_reproduction_reports_reproducible_when_offered_hashes_survive(tmp_path):
    """确定性复现：失败时 offered 的块哈希全部仍在重检索结果中 → reproducible=True。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    offered = (md5("chunk-a"), md5("chunk-b"))
    member = add_member(store, cluster, question="问题一", hashes=offered)
    retriever = FakeRetriever({"问题一": (Chunk("chunk-a"), Chunk("chunk-b"), Chunk("chunk-c"))})

    pack = assemble_evidence(member, cluster, store=store, retrieve=retriever)

    assert pack.reproduction.reproducible is True
    assert pack.reproduction.missing_hashes == ()
    assert "模型" in pack.reproduction.note  # 检索级复现，不含模型判定
    assert pack.recall_hashes == tuple(sorted({md5("chunk-a"), md5("chunk-b"), md5("chunk-c")}))
    # One member observation means exactly one re-retrieval, kb taken from the row.
    assert retriever.calls == [("问题一", "kb1")]


def test_reproduction_records_missing_hashes_when_chunks_disappear(tmp_path):
    """块消失 → reproducible=False 且记录缺失哈希；不可复现时 pack 仍生成。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    offered = (md5("chunk-a"), md5("chunk-b"))
    member = add_member(store, cluster, question="问题一", hashes=offered)
    retriever = FakeRetriever({"问题一": (Chunk("chunk-a"),)})  # chunk-b 已不在结果中

    pack = assemble_evidence(member, cluster, store=store, retrieve=retriever)

    assert isinstance(pack, EvidencePack)  # 不可复现不阻止装配，草案必须能标注 false
    assert pack.reproduction.reproducible is False
    assert pack.reproduction.missing_hashes == (md5("chunk-b"),)
    assert "模型" in pack.reproduction.note


def test_reproduction_without_retriever_says_no_retriever(tmp_path):
    """retrieve 为 None → reproduction=ReproductionResult(False, (), "no retriever")。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    offered = (md5("chunk-a"),)
    member = add_member(store, cluster, question="问题一", hashes=offered)

    pack = assemble_evidence(member, cluster, store=store)

    assert pack.recall_hashes == ()
    assert pack.reproduction.reproducible is False
    assert pack.reproduction.missing_hashes == ()
    assert pack.reproduction.note == "no retriever"
    assert pack.failed_hashes == tuple(offered)  # 失败证据不依赖重检索


def test_recall_hashes_cap_the_prompt_prefix_at_twelve(tmp_path):
    """每个观测取前 24 个块的文本 MD5 供复现，prompt-facing pack 只带前 12 条。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="q")
    retriever = FakeRetriever({"q": tuple(Chunk(f"a-{i}") for i in range(30))})

    pack = assemble_evidence(member, cluster, store=store, retrieve=retriever)

    # The reproduction check sees the first 24; the prompt-facing pack carries a
    # deterministic prefix of it (hash lists are prompt tokens).
    assert md5("a-24") not in pack.recall_hashes  # 第 25 名起不入复现并集
    assert len(pack.recall_hashes) == 12
    recall_union = sorted(md5(f"a-{i}") for i in range(24))
    assert list(pack.recall_hashes) == recall_union[:12]


def test_reproduction_check_uses_the_full_recall_union_even_when_the_pack_is_capped(tmp_path):
    """A chunk beyond the prompt cap still counts towards a reproducible verdict."""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="q", hashes=(md5("h-late"),))
    retriever = FakeRetriever({
        "q": (Chunk("keep-0"), Chunk("keep-1"), Chunk("h-late")),
    })

    pack = assemble_evidence(member, cluster, store=store, retrieve=retriever)

    assert pack.reproduction.reproducible is True
    # Three hashes fit under the cap; the point is that the verdict used the full union.
    assert len(pack.recall_hashes) == 3
    assert md5("h-late") in pack.recall_hashes


def test_success_traces_take_recent_matching_non_blocked_rows(tmp_path):
    """成功轨迹：问题与观测相同、answer 非空、route 非 blocked 的最近 3 条。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="问题一")
    tracker = FakeTracker([
        {"question": "问题一", "route": "rag", "answer": "x" * 250, "chunk_hashes": [md5("m1")]},
        {"question": "问题一", "route": "blocked", "answer": "refused", "chunk_hashes": []},
        {"question": "问题一", "route": "rag", "answer": "   ", "chunk_hashes": []},
        {"question": "无关问题", "route": "rag", "answer": "ok", "chunk_hashes": []},
        {"question": "问题一", "route": "rag", "answer": "older", "chunk_hashes": []},
        {"question": "问题一", "route": "rag", "answer": "oldest", "chunk_hashes": []},
    ])

    pack = assemble_evidence(member, cluster, store=store, tracker=tracker)

    assert [t["answer"] for t in pack.success_traces] == ["x" * 200, "older", "oldest"]
    first = pack.success_traces[0]
    assert first["question"] == "问题一"
    assert first["route"] == "rag"
    assert first["chunk_hashes"] == (md5("m1"),)
    assert first["answer_source"] == "system_history"  # 系统历史回答，非管理员确认答案
    assert first["priority"] == "system_trace"
    assert tracker.requested_limit is not None and tracker.requested_limit >= 3


def test_success_traces_skip_rows_from_another_kb(tmp_path):
    """计划第 2 步：成功轨迹只取同 kb；execution_logs 有 kb_id 时按观测 kb 过滤。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store, kb_id="kb1")
    member = add_member(store, cluster, question="问题一", kb_id="kb1")
    tracker = FakeTracker([
        {"question": "问题一", "route": "rag", "answer": "a", "kb_id": "kb2"},
        {"question": "问题一", "route": "rag", "answer": "b", "kb_id": "kb1"},
    ])

    pack = assemble_evidence(member, cluster, store=store, tracker=tracker)

    assert [t["answer"] for t in pack.success_traces] == ["b"]


def test_missing_tracker_yields_empty_traces_without_error(tmp_path):
    """tracker 缺省为空而非报错。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="问题一")

    pack = assemble_evidence(member, cluster, store=store)

    assert pack.success_traces == ()
