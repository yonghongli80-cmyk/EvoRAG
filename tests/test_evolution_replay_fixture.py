"""Golden replay fixture：四个外部 seam 的录制与重放（无凭据）。

动机（实测）：沙箱剥掉所有 `RAG_*`（含凭据），而这条链路有四处出网调用——嵌入、重排、路由模型调用、
查询改写——所以层 2 在沙箱里**永远**报 "replay unavailable"，回归 A/B 只能搬到宿主手工跑。
本模块是文档里早写明的回程：在有凭据的地方录一次，在沙箱里重放。

两条性质比"能录"本身更要紧：
**缺答案必须失败**（strict 抛 `FixtureMiss`，绝不静默回退到网络或编一个值——会静默降级的门禁等于说谎）；
**过期 fixture 必须被拒**（记录了语料指纹与四个 seam 模块的源码哈希：改过 reranker 的补丁会让录下的
评分不再是"被打补丁代码"的证据，此时 `stale_reason` 报出来，调用方必须当作不可用）。
"""

from __future__ import annotations

import hashlib

import pytest

from rag.evolution.replay_fixture import (
    FIXTURE_VERSION,
    SEAM_MODULES,
    FixtureMiss,
    ReplayFixture,
    corpus_fingerprint,
    install,
    normalize,
    seam_hashes,
    snapshot_corpus_fingerprint,
)


class _Chunk:
    def __init__(self, doc_name, text=""):
        self.doc_name = doc_name
        self.text = text


def _seams() -> dict[str, str]:
    """A complete, matching seam record: C-02 treats a missing one as stale."""
    return {name: f"hash-{index}" for index, name in enumerate(SEAM_MODULES)}


def _fixture(**overrides) -> ReplayFixture:
    base = dict(
        embeddings={hashlib.sha256(b"hello").hexdigest(): [0.1, 0.2]},
        reranks={},
        routes={normalize("算一下增长率"): "agent"},
        rewrites={normalize("年假是多少"): "年假有多少天"},
        corpus="corpus-hash",
        base_commit="a" * 40,
        seams=_seams(),
        created_at=123.0,
        version=FIXTURE_VERSION,
    )
    base.update(overrides)
    return ReplayFixture(**base)


def test_recorded_answers_are_served_by_key(tmp_path):
    fixture = _fixture()
    path = tmp_path / "fixture.json"
    fixture.save(path)
    loaded = ReplayFixture.load(path)

    assert loaded.embed(["hello"]) == [[0.1, 0.2]]
    assert loaded.route("算一下增长率  ") == "agent"
    assert loaded.rewrite("年假是多少") == "年假有多少天"
    assert loaded.version == FIXTURE_VERSION


def test_a_missing_answer_raises_instead_of_reaching_the_network():
    """严格模式：没录到的调用必须失败，绝不静默回退。"""
    fixture = _fixture()

    with pytest.raises(FixtureMiss):
        fixture.embed(["never recorded"])
    with pytest.raises(FixtureMiss):
        fixture.route("没录过的问题")
    with pytest.raises(FixtureMiss):
        fixture.rerank("q", ["a", "b"])


def test_the_rerank_key_covers_the_query_and_the_exact_text_order():
    key = hashlib.sha256("\x00".join(["q", "a", "b"]).encode()).hexdigest()
    fixture = _fixture(reranks={key: [{"index": 0, "score": 0.9}]})

    assert fixture.rerank("q", ["a", "b"]) == [{"index": 0, "score": 0.9}]
    with pytest.raises(FixtureMiss):  # 顺序不同 ⇒ 不是同一次调用
        fixture.rerank("q", ["b", "a"])


def test_install_replaces_the_seams_and_uninstall_puts_them_back():
    import rag.agent
    import rag.embedder
    import rag.query_rewriter

    fixture = _fixture()
    before_embed = rag.embedder.embed
    before_route = rag.agent.route_question
    before_rewrite = rag.query_rewriter.rewrite_query

    uninstall = install(fixture)

    assert rag.embedder.embed(["hello"]) == [[0.1, 0.2]]
    assert rag.agent.route_question("算一下增长率") == "agent"
    assert rag.query_rewriter.rewrite_query("年假是多少") == "年假有多少天"
    with pytest.raises(FixtureMiss):
        rag.embedder.embed(["not recorded"])

    uninstall()

    assert rag.embedder.embed is before_embed
    assert rag.agent.route_question is before_route
    assert rag.query_rewriter.rewrite_query is before_rewrite


def test_a_non_strict_install_falls_back_to_the_original_seam(monkeypatch):
    """非严格模式回退到"安装时的原实现"——用哨兵验证，不依赖真实网络是否可达。"""
    import rag.embedder

    calls = []

    def sentinel(texts):
        calls.append(list(texts))
        return [["from-original"]]

    monkeypatch.setattr(rag.embedder, "embed", sentinel)
    uninstall = install(_fixture(), strict=False)
    try:
        assert rag.embedder.embed(["not recorded"]) == [["from-original"]]
        assert calls == [["not recorded"]]
    finally:
        uninstall()

    assert rag.embedder.embed is sentinel  # 还原的是安装时的那一份


def test_staleness_covers_version_corpus_and_seam_drift():
    fixture = _fixture()
    seams = _seams()

    assert fixture.stale_reason(corpus="corpus-hash", seams=seams) == ""
    assert "corpus changed" in fixture.stale_reason(corpus="other", seams=seams)
    changed = dict(seams, **{"rag.reranker": "hash-2"})
    reason = fixture.stale_reason(corpus="corpus-hash", seams=changed)
    assert "seam modules changed" in reason and "rag.reranker" in reason
    assert "format" in _fixture(version=99).stale_reason(corpus="corpus-hash", seams={})


def test_staleness_is_fail_closed_when_a_seam_hash_is_missing_or_unavailable():
    """C-02：少一条 seam 记录、或任一侧报 "unavailable"，都等于**没有证据** ⇒ 过期。"""
    fixture = _fixture()
    seams = _seams()

    partial = dict(seams)
    partial.pop("rag.jev.client")
    assert "no seam hash" in _fixture(seams=partial).stale_reason(
        corpus="corpus-hash", seams=seams
    )

    recorded_unavailable = dict(seams, **{"rag.reranker": "unavailable"})
    assert "unavailable" in _fixture(seams=recorded_unavailable).stale_reason(
        corpus="corpus-hash", seams=seams
    )

    actual_unavailable = dict(seams, **{"rag.agent": "unavailable"})
    assert "unavailable" in fixture.stale_reason(
        corpus="corpus-hash", seams=actual_unavailable
    )

    actual_missing = dict(seams)
    actual_missing.pop("rag.embedder")
    assert "unavailable" in fixture.stale_reason(corpus="corpus-hash", seams=actual_missing)


def test_staleness_refuses_a_fixture_or_replay_without_a_corpus_fingerprint():
    """C-02：语料指纹缺失（fixture 没记或回放侧算不出）一律过期，绝不默认相等。"""
    fixture = _fixture()
    seams = _seams()

    assert "no corpus" in _fixture(corpus="").stale_reason(corpus="corpus-hash", seams=seams)
    assert "unavailable" in fixture.stale_reason(corpus="", seams=seams)


def test_corpus_fingerprint_notices_a_changed_snapshot():
    a = corpus_fingerprint([_Chunk("x.md"), _Chunk("y.md")])
    b = corpus_fingerprint([_Chunk("x.md"), _Chunk("y.md")])
    c = corpus_fingerprint([_Chunk("x.md"), _Chunk("z.md")])

    assert a == b
    assert a != c


def test_snapshot_corpus_fingerprint_reproduces_the_recorded_one(tmp_path):
    """C-02：沙箱从只读快照算出**真实**语料指纹，才能与 fixture 记的那个比较。"""
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    snapshot = tmp_path / "snapshot"
    qdrant_dir = snapshot / "qdrant_data"
    client = QdrantClient(path=str(qdrant_dir))
    client.create_collection(
        collection_name="kb1", vectors_config=VectorParams(size=2, distance=Distance.COSINE)
    )
    client.upsert(
        collection_name="kb1",
        points=[
            PointStruct(id=1, vector=[0.1, 0.2], payload={"doc_name": "a.md"}),
            PointStruct(id=2, vector=[0.3, 0.4], payload={"doc_name": "b.md"}),
            PointStruct(id=3, vector=[0.5, 0.6], payload={"doc_name": "a.md"}),
        ],
    )
    client.close()

    expected = corpus_fingerprint([_Chunk("a.md"), _Chunk("b.md"), _Chunk("a.md")])
    # 快照目录既是 `qdrant_data/` 的父目录，也可以是语料目录本身（与 _replay_env 同一规则）。
    assert snapshot_corpus_fingerprint(str(snapshot), "kb1") == expected
    assert snapshot_corpus_fingerprint(str(qdrant_dir), "kb1") == expected

    # 读不到 ⇒ 空串（调用方必须判过期），绝不是一个"相等"的指纹。
    assert snapshot_corpus_fingerprint(str(snapshot), "missing_collection") == ""
    assert snapshot_corpus_fingerprint(str(tmp_path / "absent"), "kb1") == ""
    assert snapshot_corpus_fingerprint("", "kb1") == ""


def test_seam_hashes_describe_the_code_that_is_running():
    hashes = seam_hashes()

    # 第五个 seam（Jev/TypeSafe 客户端）也在内：录制实测 embeddings 0 证明生产路径把检索交给
    # 外部服务，缺了它就无法无凭据重放；也因此，改到它的补丁会让 fixture 判过期。
    assert set(hashes) == {
        "rag.embedder",
        "rag.reranker",
        "rag.agent",
        "rag.query_rewriter",
        "rag.jev.client",
    }
    assert all(len(digest) == 64 for digest in hashes.values())


def _preflight_decision(**overrides):
    """按 models.py 的真实字段构造（literal 取值来自 IntentKind/ComplexityKind）。"""
    from rag.jev.models import PreflightDecision

    base = dict(
        intent="retrieval_qa",
        intent_confidence=0.9,
        complexity="simple",
        complexity_confidence=0.8,
        rewrite_probability=0.1,
        model="scripted",
        question_set_version="preflight-v1",
        duration_ms=12.0,
    )
    base.update(overrides)
    return PreflightDecision(**base)


def _judgment(chunk_id, relevance, **overrides):
    from rag.jev.models import PassageJudgment

    base = dict(
        chunk_id=chunk_id,
        relevance=relevance,
        answer_evidence=relevance,
        contradiction=0.0,
        prompt_injection=0.0,
        question_set_version="passages-v1",
    )
    base.update(overrides)
    return PassageJudgment(**base)


def test_the_jev_seam_round_trips_through_the_fixture(tmp_path):
    """第五 seam：Jev 的 preflight / judge_passages 的映射结果要能原样重放。"""
    from rag.evolution.replay_fixture import dump_jev, jev_key
    from rag.jev.client import JevBatchResult

    decision = _preflight_decision()
    judgments = (_judgment("c1", 0.9), _judgment("c2", 0.2))
    state = {"question": "年假是多少", "kb_id": "kb_hr"}
    fixture = _fixture(
        jev_preflights={
            jev_key("preflight", state): dump_jev(
                JevBatchResult(value=decision, available=True, duration_ms=12.0), stage="preflight"
            )
        },
        jev_passages={
            jev_key("passages", state, ["c1", "c2"]): dump_jev(
                JevBatchResult(value=judgments, available=True, duration_ms=34.0), stage="passages"
            )
        },
    )
    path = tmp_path / "f.json"
    fixture.save(path)
    loaded = ReplayFixture.load(path)

    replayed_decision = loaded.lookup_preflight(state)
    replayed_judgments = loaded.lookup_passages(state, ["c1", "c2"])

    assert replayed_decision.available is True
    assert replayed_decision.value == decision
    assert replayed_decision.duration_ms == 12.0  # 录制的耗时被复用：Δ 里抵消，绝对值不是生产时延
    assert replayed_judgments.value == judgments
    assert replayed_judgments.duration_ms == 34.0


def test_a_failed_jev_outcome_is_replayed_as_a_failure():
    from rag.evolution.replay_fixture import dump_jev, jev_key
    from rag.jev.client import JevBatchResult

    state = {"q": 1}
    fixture = _fixture(
        jev_preflights={
            jev_key("preflight", state): dump_jev(
                JevBatchResult(available=False, failure_kind="timeout", duration_ms=150.0),
                stage="preflight",
            )
        }
    )

    replayed = fixture.lookup_preflight(state)

    assert replayed.available is False
    assert replayed.failure_kind == "timeout"
    assert replayed.value is None


def test_a_jev_call_with_no_recording_is_a_miss():
    fixture = _fixture()

    with pytest.raises(FixtureMiss):
        fixture.lookup_preflight({"never": "recorded"})
    with pytest.raises(FixtureMiss):
        fixture.lookup_passages({"never": "recorded"}, ["c1"])


def test_install_also_points_the_jev_client_at_the_fixture():
    from rag.evolution.replay_fixture import dump_jev, jev_key
    from rag.jev.client import JevBatchResult, JevClient

    state = {"question": "q"}
    fixture = _fixture(
        jev_preflights={
            jev_key("preflight", state): dump_jev(
                JevBatchResult(value=_preflight_decision(), available=True, duration_ms=1.0),
                stage="preflight",
            )
        }
    )
    before = JevClient.preflight

    uninstall = install(fixture)
    try:
        client = JevClient.__new__(JevClient)  # 不构造真实客户端（那需要凭据）
        assert client.preflight(state).available is True
        with pytest.raises(FixtureMiss):
            client.preflight({"other": "state"})
    finally:
        uninstall()

    assert JevClient.preflight is before
