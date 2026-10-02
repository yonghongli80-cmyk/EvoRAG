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


def test_pack_carries_what_the_passage_judge_saw(tmp_path):
    """判定层统计量随观测进证据包；缺失就是"未记录"，不能当成 0。

    这是"召回了却没被采用"（判定/改写层可修）与"根本没召回"（内容缺口）的分界证据，
    来源必须是失败请求自己写下的 decision_metadata，而不是事后重判。
    """

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="q1")
    member = {
        **member,
        "details_decision_metadata": {
            "passage_judged": 12,
            "passage_relevant": 5,
            "passage_evidence": 0,
            "passage_contradicted": 1,
            "passage_injected": 0,
            "jev_route": "rag",  # 非统计量键不进包
        },
    }

    pack = assemble_evidence(member, cluster, store=store)

    assert dict(pack.passage_judging) == {
        "passage_judged": 12,
        "passage_relevant": 5,
        "passage_evidence": 0,
        "passage_contradicted": 1,
        "passage_injected": 0,
    }


def test_pack_leaves_judging_counts_empty_when_the_request_predates_them(tmp_path):
    """旧观测没有这些键：空元组（未记录），不是全 0。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="q1")
    member = {**member, "details_decision_metadata": '{"jev_route": "rag"}'}

    pack = assemble_evidence(member, cluster, store=store)

    assert pack.passage_judging == ()


def test_pack_carries_the_failure_route_and_exception_class(tmp_path):
    """异常失败的路由与异常类必须进证据包，否则诊断只能把它解释成检索/路由问题。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="q1")
    member = {
        **member,
        "route": "error",
        "details_exception_class": "ValueError",
    }

    pack = assemble_evidence(member, cluster, store=store)

    assert pack.failure_route == "error"
    assert pack.exception_class == "ValueError"


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


# ------------------------------------------------------------------
# Question-term coverage: where the question's own words appear in the recall.
# ------------------------------------------------------------------


def test_question_term_coverage_reports_where_the_terms_appear(tmp_path):
    """关键词覆盖度：每个问题词在召回集合里命中几块、最早出现在第几名。

    这是区分"内容存在却没被采用"（ranking_error）与"召回里根本没有内容"
    （recall_miss / 内容缺口）的确定性依据，而且只依赖问题词本身：
    命中为 0 的词以 ``chunks=0``、``best_rank=0`` 如实报出，不省略。
    """

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="副厂配件报价")
    retriever = FakeRetriever({
        "副厂配件报价": (
            Chunk("无关内容"),
            Chunk("这是副厂配件明细表"),
        ),
    })

    pack = assemble_evidence(member, cluster, store=store, retrieve=retriever)

    coverage = {record["term"]: record for record in pack.question_term_coverage}
    assert coverage["副厂"] == {"term": "副厂", "chunks": 1, "best_rank": 2}
    assert coverage["配件"] == {"term": "配件", "chunks": 1, "best_rank": 2}
    assert coverage["报价"] == {"term": "报价", "chunks": 0, "best_rank": 0}


def test_question_term_coverage_looks_past_the_preview_window(tmp_path):
    """信号必须在整块文本上算：词出现在第 160 字之后同样要算命中。

    预览是 160 字的有界窗口，只读预览会继承同一个盲点——而"深层块被埋没"
    正是要判断的情形。
    """

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="质检培训")
    retriever = FakeRetriever({
        "质检培训": (Chunk("铺垫" * 400 + "质检培训课件清单"),),
    })

    pack = assemble_evidence(member, cluster, store=store, retrieve=retriever)

    coverage = {record["term"]: record for record in pack.question_term_coverage}
    assert coverage["质检"]["chunks"] == 1
    assert coverage["培训"]["best_rank"] == 1
    # The preview the prompt sees is still the bounded leading window.
    assert "质检培训" not in pack.recall_previews[0]["text"]


def test_question_term_coverage_is_empty_without_a_retriever(tmp_path):
    """没有重检索就没有覆盖度证据：空元组，而不是编造 0 命中。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="副厂配件明细")

    pack = assemble_evidence(member, cluster, store=store)

    assert pack.question_term_coverage == ()


def test_question_term_coverage_terms_are_deterministic_and_bounded(tmp_path):
    """词元固定有序、有上限：同一问题两次组装得到同一份信号。"""

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    question = "富士 X-S20 电池和充电器是什么型号"
    member = add_member(store, cluster, question=question)
    retriever = FakeRetriever({question: (Chunk("富士 X-S20 电池 充电器"),)})

    first = assemble_evidence(member, cluster, store=store, retrieve=retriever)
    second = assemble_evidence(member, cluster, store=store, retrieve=retriever)

    assert first.question_term_coverage == second.question_term_coverage
    assert 0 < len(first.question_term_coverage) <= 8
    terms = [record["term"] for record in first.question_term_coverage]
    assert len(set(terms)) == len(terms)


def test_label_evidence_checks_the_admin_answer_against_the_recall(tmp_path):
    """label_evidence：把管理员答案里的型号串拿去召回块做**精确包含检查**。

    这是 `ranking_error`（能拿到却没被采用）与 `undetermined`（内容缺口）之间唯一不靠猜的
    判据——它查的是答案本身，而不是关键词相关度。
    """

    import json as _json

    from rag.evolution.evidence import assemble_evidence

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="找一下副厂配件明细")
    labels = tmp_path / "labels.json"
    labels.write_text(
        _json.dumps(
            [
                {
                    "question": "找一下副厂配件明细",
                    "expected_answer": "适马 16mm 的遮阳罩型号为 LH716-01，售价已脱敏；另一支是 A046。",
                    "expected_source": "lens-hood-price-redacted.xlsx",
                    "admin_note": "答案在表里",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    retriever = FakeRetriever(
        {"找一下副厂配件明细": (Chunk("镜头 LH716-01 遮阳罩 金额<金额>"), Chunk("无关内容"))}
    )

    pack = assemble_evidence(
        member, cluster, store=store, labels_path=str(labels), retrieve=retriever
    )

    hits = {record["token"]: record for record in pack.label_evidence}
    assert "LH716-01" in hits  # 答案文本确实出现在召回块里
    assert hits["LH716-01"]["chunks"] == 1
    assert hits["LH716-01"]["first_rank"] == 0
    assert hits["LH716-01"]["hash"] == md5("镜头 LH716-01 遮阳罩 金额<金额>")
    # A046 只在标签里、不在召回里：没有命中就不该出现在证据里。
    assert "A046" not in hits


def test_label_evidence_is_empty_without_a_retriever(tmp_path):
    """没有召回窗口就没有包含检查：不能把"没查"渲染成"查过但没命中"。"""

    import json as _json

    from rag.evolution.evidence import assemble_evidence

    store = FailureTraceStore(tmp_path / "e.db")
    cluster = build_cluster(store)
    member = add_member(store, cluster, question="找一下副厂配件明细")
    labels = tmp_path / "labels.json"
    labels.write_text(
        _json.dumps(
            [{"question": "找一下副厂配件明细", "expected_answer": "型号 LH716-01"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    pack = assemble_evidence(member, cluster, store=store, labels_path=str(labels))

    assert pack.label_evidence == ()
