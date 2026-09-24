"""Task 2: deterministic-first evidence assembly with a reproduction check.

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
from rag.evolution.models import EvidencePack, FailureObservation
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
    questions: list[str],
    hashes_by_question: dict[str, tuple[str, ...]] | None = None,
    kb_id: str = "kb1",
    failure_stage: str = "retrieval",
    root_cause_feature: str = "recall_miss",
) -> dict:
    """Build one cluster with the given members using only existing store methods."""

    cluster = store.upsert_cluster(
        cluster_key=f"{kb_id}|{failure_stage}|{root_cause_feature}",
        kb_id=kb_id,
        failure_stage=failure_stage,
        root_cause_feature=root_cause_feature,
        seen_at=time.time(),
    )
    for question in questions:
        row = store.add_observation(
            FailureObservation(
                kb_id=kb_id,
                failure_stage=failure_stage,
                question=question,
                route="rag",
                chunk_hashes=(hashes_by_question or {}).get(question, ()),
                best_score=0.05,
                answerability_kind="insufficient_evidence",
            )
        )
        store.attach_cluster_member(
            row["id"],
            cluster["id"],
            failure_stage=failure_stage,
            root_cause_feature=root_cause_feature,
            now=time.time(),
        )
    return cluster


def write_labels(path, entries: list[dict]) -> None:
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def test_admin_labels_enter_the_pack_with_priority(tmp_path):
    """设计第 6 节：管理员确认标签是最优先级证据，进 pack 且标注优先级。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store, questions=["年假额度 剩几天", "怎么申请报销"])
    labels_path = tmp_path / "labels.json"
    write_labels(labels_path, [
        {"case_id": "real-001", "question": "年假额度剩几天", "expected_route": "rag",
         "expected_source_names": ["假期制度.md"], "expected_answer": "每年 10 天"},
        {"case_id": "real-002", "question": "无关问题", "expected_route": "rag",
         "expected_source_names": [], "expected_answer": ""},
    ])

    pack = assemble_evidence(cluster, store=store, labels_path=labels_path)

    # 仅问题规范化匹配的标签进入 pack，且原始字段与优先级标注并存。
    assert [label["case_id"] for label in pack.admin_labels] == ["real-001"]
    label = pack.admin_labels[0]
    assert label["priority"] == "admin_confirmed"
    assert label["expected_route"] == "rag"
    assert label["expected_source_names"] == ["假期制度.md"]


def test_label_matching_normalizes_case_and_whitespace(tmp_path):
    """问题文本规范化：strip + 去全部空白 + 小写化后才比较。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store, questions=["Qdrant  故障 Fu"])
    labels_path = tmp_path / "labels.json"
    write_labels(labels_path, [
        {"case_id": "real-009", "question": " qdrant 故障fu ", "expected_route": "rag"},
    ])

    pack = assemble_evidence(cluster, store=store, labels_path=labels_path)

    assert [label["case_id"] for label in pack.admin_labels] == ["real-009"]


def test_admin_labels_empty_without_a_labels_file(tmp_path):
    """缺省 / 文件不存在 / 空列表 → admin_labels 为空 tuple，不报错。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store, questions=["q"])

    assert assemble_evidence(cluster, store=store).admin_labels == ()
    assert assemble_evidence(
        cluster, store=store, labels_path=tmp_path / "missing.json"
    ).admin_labels == ()
    empty_path = tmp_path / "empty.json"
    empty_path.write_text("[]", encoding="utf-8")
    assert assemble_evidence(cluster, store=store, labels_path=empty_path).admin_labels == ()


def test_pack_carries_cluster_signals_and_failed_hashes(tmp_path):
    """pack 透传聚类权威信号；failed_hashes 是成员 offered 块哈希的并集。"""

    store = FailureTraceStore(tmp_path / "e.db")
    offered_a = (md5("a1"), md5("a2"))
    offered_b = (md5("b1"),)
    cluster = build_cluster(
        store,
        questions=["q1", "q2"],
        hashes_by_question={"q1": offered_a, "q2": offered_b},
    )

    pack = assemble_evidence(cluster, store=store)

    assert isinstance(pack, EvidencePack)
    assert pack.cluster_id == cluster["id"]
    assert pack.kb_id == "kb1"
    assert pack.failure_stage == "retrieval"
    assert pack.root_cause_feature == "recall_miss"
    assert pack.failed_hashes == tuple(sorted(offered_a + offered_b))
    # 成员问题去重（规范化）后最多 3 条，保持首见顺序。
    assert pack.question_samples == ("q1", "q2")


def test_question_samples_are_normalized_deduped_and_capped_at_three(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store, questions=["q1", "q1 ", "q2", "q3", "q4"])

    pack = assemble_evidence(cluster, store=store)

    assert pack.question_samples == ("q1", "q2", "q3")


def test_reproduction_reports_reproducible_when_offered_hashes_survive(tmp_path):
    """确定性复现：失败时 offered 的块哈希全部仍在重检索结果中 → reproducible=True。"""

    store = FailureTraceStore(tmp_path / "e.db")
    offered = (md5("chunk-a"), md5("chunk-b"))
    cluster = build_cluster(store, questions=["问题一"], hashes_by_question={"问题一": offered})
    retriever = FakeRetriever({"问题一": (Chunk("chunk-a"), Chunk("chunk-b"), Chunk("chunk-c"))})

    pack = assemble_evidence(cluster, store=store, retrieve=retriever)

    assert pack.reproduction.reproducible is True
    assert pack.reproduction.missing_hashes == ()
    assert "模型" in pack.reproduction.note  # 检索级复现，不含模型判定
    assert pack.recall_hashes == tuple(sorted({md5("chunk-a"), md5("chunk-b"), md5("chunk-c")}))
    assert retriever.calls == [("问题一", "kb1")]  # 每个 sample 一次重检索，kb 取自聚类


def test_reproduction_records_missing_hashes_when_chunks_disappear(tmp_path):
    """块消失 → reproducible=False 且记录缺失哈希；不可复现时 pack 仍生成。"""

    store = FailureTraceStore(tmp_path / "e.db")
    offered = (md5("chunk-a"), md5("chunk-b"))
    cluster = build_cluster(store, questions=["问题一"], hashes_by_question={"问题一": offered})
    retriever = FakeRetriever({"问题一": (Chunk("chunk-a"),)})  # chunk-b 已不在结果中

    pack = assemble_evidence(cluster, store=store, retrieve=retriever)

    assert isinstance(pack, EvidencePack)  # 不可复现不阻止装配，草案必须能标注 false
    assert pack.reproduction.reproducible is False
    assert pack.reproduction.missing_hashes == (md5("chunk-b"),)
    assert "模型" in pack.reproduction.note


def test_reproduction_without_retriever_says_no_retriever(tmp_path):
    """retrieve 为 None → reproduction=ReproductionResult(False, (), "no retriever")。"""

    store = FailureTraceStore(tmp_path / "e.db")
    offered = (md5("chunk-a"),)
    cluster = build_cluster(store, questions=["问题一"], hashes_by_question={"问题一": offered})

    pack = assemble_evidence(cluster, store=store)

    assert pack.recall_hashes == ()
    assert pack.reproduction.reproducible is False
    assert pack.reproduction.missing_hashes == ()
    assert pack.reproduction.note == "no retriever"
    assert pack.failed_hashes == tuple(offered)  # 失败证据不依赖重检索


def test_recall_hashes_use_the_first_24_chunks_per_question(tmp_path):
    """每个 sample 取前 24 个块的文本 MD5，跨 sample 取并集。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store, questions=["q1", "q2"])
    retriever = FakeRetriever({
        "q1": tuple(Chunk(f"a-{i}") for i in range(30)),
        "q2": tuple(Chunk(f"b-{i}") for i in range(26)),
    })

    pack = assemble_evidence(cluster, store=store, retrieve=retriever)

    assert len(pack.recall_hashes) == 48  # 24 + 24，两个问题的前 24 名互不重叠
    assert md5("a-23") in pack.recall_hashes
    assert md5("a-24") not in pack.recall_hashes  # 第 25 名起不入 pack
    assert md5("b-25") not in pack.recall_hashes


def test_success_traces_take_recent_matching_non_blocked_rows(tmp_path):
    """成功轨迹：问题与成员相同、answer 非空、route 非 blocked 的最近 3 条。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store, questions=["问题一", "问题二"])
    tracker = FakeTracker([
        {"question": "问题一", "route": "rag", "answer": "x" * 250, "chunk_hashes": [md5("m1")]},
        {"question": "问题二", "route": "rag", "answer": "ok", "chunk_hashes": [md5("m2")]},
        {"question": "问题一", "route": "blocked", "answer": "refused", "chunk_hashes": []},
        {"question": "问题一", "route": "rag", "answer": "   ", "chunk_hashes": []},
        {"question": "无关问题", "route": "rag", "answer": "ok", "chunk_hashes": []},
        {"question": "问题一", "route": "rag", "answer": "older", "chunk_hashes": []},
        {"question": "问题一", "route": "rag", "answer": "oldest", "chunk_hashes": []},
    ])

    pack = assemble_evidence(cluster, store=store, tracker=tracker)

    assert [t["answer"] for t in pack.success_traces] == ["x" * 200, "ok", "older"]
    first = pack.success_traces[0]
    assert first["question"] == "问题一"
    assert first["route"] == "rag"
    assert first["chunk_hashes"] == (md5("m1"),)
    assert first["answer_source"] == "system_history"  # 系统历史回答，非管理员确认答案
    assert first["priority"] == "system_trace"
    assert tracker.requested_limit is not None and tracker.requested_limit >= 6


def test_success_traces_skip_rows_from_another_kb(tmp_path):
    """计划第 2 步：成功轨迹只取同 kb；execution_logs 有 kb_id 时按聚类 kb 过滤。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store, questions=["问题一"], kb_id="kb1")
    tracker = FakeTracker([
        {"question": "问题一", "route": "rag", "answer": "a", "kb_id": "kb2"},
        {"question": "问题一", "route": "rag", "answer": "b", "kb_id": "kb1"},
    ])

    pack = assemble_evidence(cluster, store=store, tracker=tracker)

    assert [t["answer"] for t in pack.success_traces] == ["b"]


def test_missing_tracker_yields_empty_traces_without_error(tmp_path):
    """tracker 缺省为空而非报错。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store, questions=["问题一"])

    pack = assemble_evidence(cluster, store=store)

    assert pack.success_traces == ()
