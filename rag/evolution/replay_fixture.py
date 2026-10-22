"""Golden replay fixture: the four external seams, recorded once, replayed without credentials.

The sandbox strips every ``RAG_*`` setting, credentials included, while this pipeline reaches the
network in four places — the embedding call, the rerank call, the router's model call and the
query rewrite. Layer 2 could therefore never run inside the sandbox: it reported "replay
unavailable" every time, and the regression A/B moved out to the administrator's machine. This
module is the documented way back in: record those four answers once, where credentials exist,
then let the sandbox replay them.

Two properties matter more than the recording itself.

**A missing answer is a failure, never a fallback.** ``strict`` lookups raise :class:`FixtureMiss`
rather than quietly calling the network or returning something plausible, because a replay that
silently degrades is a gate that lies.

**A stale fixture is refused, not trusted.** A fixture records the corpus fingerprint and the
source hash of each seam module. A reranker patch changes ``rag/reranker.py``, which means the
recorded scores are no longer evidence about the patched code — so ``stale_reason`` reports it and
the caller must treat the replay as unavailable. That is the price of never letting a recorded
answer stand in for the behaviour under test; patches that touch a seam cannot be measured this
way and have to be measured where the credentials are.

The freshness check is **fail-closed** (2026-10-22, C-02): a fixture that does not record one of
the seam hashes, a side that reports a hash as ``"unavailable"``, or a corpus fingerprint missing
on either side is stale — an absent answer is never an equal answer. The corpus argument must be
the fingerprint of the corpus the replay *actually reads*
(:func:`snapshot_corpus_fingerprint`), never the fixture's own value: comparing the recording with
itself is how a stale corpus used to pass.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Environment variable that switches the replay seams onto a fixture.
FIXTURE_ENV = "RAG_EVOLUTION_REPLAY_FIXTURE"

#: Fixture format version; a mismatch is stale by definition.
FIXTURE_VERSION = 1

#: The four seam modules whose source identity is recorded with the answers.
SEAM_MODULES = (
    "rag.embedder",
    "rag.reranker",
    "rag.agent",
    "rag.query_rewriter",
    # 第五个 seam：Jev 生产路径把检索交给外部 TypeSafe 服务，客户端带 typesafe_api_key。
    # 录制实测（embeddings 0）证明这条路径连本地嵌入器都不经过，所以缺了它就无法无凭据重放。
    "rag.jev.client",
)

#: Repository-relative POSIX path of every seam module. A sandbox uses these to answer "does this
#: diff touch a seam at all?" *before* it hashes anything, because a recorded answer can never
#: describe a patched seam.
SEAM_PATHS: tuple[str, ...] = tuple(
    str(Path(*name.split(".")).with_suffix(".py")).replace("\\", "/") for name in SEAM_MODULES
)

#: Scroll page size when fingerprinting a Qdrant snapshot: bounded memory for a corpus read.
CORPUS_SCROLL_LIMIT = 10_000


logger = logging.getLogger(__name__)


class FixtureMiss(RuntimeError):
    """The fixture has no answer for a call: the replay cannot continue as evidence."""


def _key(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _rerank_key(query: str, texts: Sequence[str]) -> str:
    return _key("\x00".join([str(query), *[str(text) for text in texts]]))


def jev_key(stage: str, state: object, chunk_ids: Sequence[str] | None = None) -> str:
    """Deterministic key for one Jev call: stage, state and candidate ids.

    ``default=str`` keeps a state that carries something exotic from breaking the recording while
    staying deterministic for the dicts the pipeline actually passes.
    """
    return _key(
        json.dumps(
            {"stage": stage, "state": state, "chunk_ids": list(chunk_ids or ())},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    )


def normalize(question: str) -> str:
    """Whitespace-insensitive question key, matching how the labels file is matched."""
    return " ".join(str(question or "").split())


def _corpus_key(count: int, names: Sequence[str]) -> str:
    """The one fingerprint shape: point count plus the sorted set of document names."""
    return _key("\x00".join([str(int(count)), *sorted({str(name) for name in names})]))


def corpus_fingerprint(chunks: Sequence[object]) -> str:
    """A cheap identity for the corpus a recording was made against.

    Point count plus the sorted set of document names: enough to notice that the snapshot the
    replay reads is not the snapshot the recording was made from, without hashing gigabytes.
    """
    names = [str(getattr(chunk, "doc_name", "") or "") for chunk in chunks]
    return _corpus_key(len(chunks), names)


def snapshot_corpus_fingerprint(
    snapshot: str | Path | None, collection_name: str
) -> str:
    """The fingerprint of the corpus a replay will read from a Qdrant snapshot, or ``""``.

    The fixture records the fingerprint of the corpus it was recorded against, computed with
    :func:`corpus_fingerprint`; a replay is evidence about a candidate only when it reads *that*
    corpus. The sandbox therefore recomputes the same shape from the read-only snapshot it hands
    the replay, and passes the result to :meth:`ReplayFixture.stale_reason`.

    ``""`` means "could not fingerprint" — never "matched". Callers must treat it as stale, so an
    unreadable or absent snapshot can never be mistaken for a fresh recording.

    ``snapshot`` follows the same rule as the replay environment: a directory that either *is*
    the Qdrant storage or carries it as ``qdrant_data/``.
    """
    root = str(snapshot or "").strip()
    collection = str(collection_name or "").strip()
    if not root or not collection or not Path(root).is_dir():
        return ""
    path = Path(root)
    corpus_dir = path / "qdrant_data"
    if corpus_dir.is_dir():
        path = corpus_dir
    client = None
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(path=str(path))
        names: list[str] = []
        count = 0
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=collection,
                limit=CORPUS_SCROLL_LIMIT,
                offset=offset,
                with_payload=["doc_name"],
            )
            count += len(points)
            names.extend(str((point.payload or {}).get("doc_name", "") or "") for point in points)
            if offset is None:
                break
        return _corpus_key(count, names)
    except Exception:  # noqa: BLE001 - an unreadable snapshot means "no fingerprint", not a crash
        logger.warning("replay fixture: could not fingerprint the corpus snapshot")
        return ""
    finally:
        if client is not None:
            # Closing must never mask the fingerprint, which is why it is suppressed rather than
            # allowed to replace the return value (or the empty string on failure).
            with contextlib.suppress(Exception):
                client.close()


def seam_hashes(root: str | Path | None = None) -> dict[str, str]:
    """Source hash per seam module.

    With ``root`` the files are read from that checkout — a sandbox parent uses this to ask "does
    this worktree still match what the fixture recorded?" without importing anything from it.
    Without it the modules are imported, so the hash describes the code that is actually running.
    """
    hashes: dict[str, str] = {}
    for name in SEAM_MODULES:
        relative = Path(*name.split(".")).with_suffix(".py")
        try:
            if root is not None:
                source = (Path(root) / relative).read_text(encoding="utf-8", errors="replace")
            else:
                import importlib

                module = importlib.import_module(name)
                source = Path(module.__file__ or "").read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - a missing module is recorded as such, never fatal
            hashes[name] = "unavailable"
            continue
        hashes[name] = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return hashes


@dataclass(frozen=True)
class ReplayFixture:
    """Recorded answers for the four seams, plus what they were recorded against."""

    embeddings: dict[str, list[float]] = field(default_factory=dict)
    reranks: dict[str, list] = field(default_factory=dict)
    jev_preflights: dict[str, dict] = field(default_factory=dict)
    jev_passages: dict[str, dict] = field(default_factory=dict)
    routes: dict[str, str] = field(default_factory=dict)
    rewrites: dict[str, str] = field(default_factory=dict)
    corpus: str = ""
    base_commit: str = ""
    seams: dict[str, str] = field(default_factory=dict)
    created_at: float = 0.0
    version: int = FIXTURE_VERSION

    # ---- lookup -------------------------------------------------------------

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """The recorded vectors, in order; a text with no recording is a miss."""
        vectors: list[list[float]] = []
        for text in texts:
            key = _key(text)
            if key not in self.embeddings:
                raise FixtureMiss(f"no recorded embedding for text hash {key[:12]}")
            vectors.append(list(self.embeddings[key]))
        return vectors

    def rerank(self, query: str, texts: Sequence[str]) -> list:
        """The recorded rerank answer for exactly these texts, in this order."""
        key = _rerank_key(query, texts)
        if key not in self.reranks:
            raise FixtureMiss(f"no recorded rerank for query hash {key[:12]}")
        return json.loads(json.dumps(self.reranks[key]))

    def lookup_preflight(self, state: object):
        """The recorded preflight outcome for exactly this state."""
        key = jev_key("preflight", state)
        if key not in self.jev_preflights:
            raise FixtureMiss(f"no recorded Jev preflight for state hash {key[:12]}")
        return _rebuild_jev(self.jev_preflights[key], "preflight")

    def lookup_passages(self, state: object, chunk_ids: Sequence[str]):
        """The recorded passage judgment for exactly this state and candidate list."""
        key = jev_key("passages", state, chunk_ids)
        if key not in self.jev_passages:
            raise FixtureMiss(f"no recorded Jev passages for state hash {key[:12]}")
        return _rebuild_jev(self.jev_passages[key], "passages")

    def route(self, question: str) -> str:
        key = normalize(question)
        if key not in self.routes:
            raise FixtureMiss(f"no recorded route for question {key[:24]!r}")
        return self.routes[key]

    def rewrite(self, question: str) -> str:
        key = normalize(question)
        if key not in self.rewrites:
            raise FixtureMiss(f"no recorded rewrite for question {key[:24]!r}")
        return self.rewrites[key]

    # ---- freshness ----------------------------------------------------------

    def stale_reason(self, *, corpus: str, seams: Mapping[str, str]) -> str:
        """Why this fixture must not be used, or ``""`` when it may be.

        The seam comparison is the important one: recorded answers describe the code that was
        running when they were recorded, so a patch to a seam module (the reranker is the usual
        one) makes them evidence about the wrong code.

        Every input the recording cannot vouch for is **stale**, not skipped: a missing format,
        a corpus fingerprint missing on either side or differing, a seam hash this fixture never
        recorded, a seam hash either side reports as ``"unavailable"``, and any recorded/actual
        difference. An absent answer is never an equal answer (C-02, 2026-10-22).
        """
        if int(self.version) != FIXTURE_VERSION:
            return f"fixture format v{self.version} != v{FIXTURE_VERSION}"
        recorded_corpus = str(self.corpus or "").strip()
        actual_corpus = str(corpus or "").strip()
        if not recorded_corpus:
            return "the fixture records no corpus fingerprint"
        if not actual_corpus:
            return "the replay corpus fingerprint is unavailable"
        if recorded_corpus != actual_corpus:
            return "corpus changed since the fixture was recorded"

        recorded = {
            str(name): str(digest or "").strip() for name, digest in (self.seams or {}).items()
        }
        actual = {str(name): str(digest or "").strip() for name, digest in (seams or {}).items()}
        missing = sorted(name for name in SEAM_MODULES if not recorded.get(name))
        if missing:
            return "the fixture records no seam hash for: " + ", ".join(missing)
        unavailable = sorted(
            name
            for name in SEAM_MODULES
            if not actual.get(name)
            or recorded.get(name) == "unavailable"
            or actual.get(name) == "unavailable"
        )
        if unavailable:
            return "seam hashes unavailable for: " + ", ".join(unavailable)
        changed = sorted(name for name in SEAM_MODULES if recorded.get(name) != actual.get(name))
        if changed:
            return "seam modules changed since the fixture was recorded: " + ", ".join(changed)
        return ""

    # ---- io -----------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        payload = {
            "version": self.version,
            "created_at": self.created_at,
            "corpus": self.corpus,
            "base_commit": self.base_commit,
            "seams": self.seams,
            "embeddings": self.embeddings,
            "reranks": self.reranks,
            "routes": self.routes,
            "rewrites": self.rewrites,
            "jev_preflights": self.jev_preflights,
            "jev_passages": self.jev_passages,
        }
        Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> ReplayFixture:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            embeddings=payload.get("embeddings") or {},
            reranks=payload.get("reranks") or {},
            routes=payload.get("routes") or {},
            rewrites=payload.get("rewrites") or {},
            jev_preflights=payload.get("jev_preflights") or {},
            jev_passages=payload.get("jev_passages") or {},
            corpus=str(payload.get("corpus") or ""),
            base_commit=str(payload.get("base_commit") or ""),
            seams=payload.get("seams") or {},
            created_at=float(payload.get("created_at") or 0.0),
            version=int(payload.get("version") or 0),
        )


def dump_jev(result, *, stage: str) -> dict:
    """JSON-safe form of one ``JevBatchResult`` (its mapped value plus the outcome fields)."""
    value = getattr(result, "value", None)
    dumped_value = None
    if value is not None:
        items = value if isinstance(value, tuple | list) else (value,)
        dumped_value = [item.model_dump(mode="json") for item in items]
    return {
        "stage": stage,
        "available": bool(getattr(result, "available", False)),
        "failure_kind": getattr(result, "failure_kind", None),
        "duration_ms": float(getattr(result, "duration_ms", 0.0) or 0.0),
        "value": dumped_value,
    }


def _rebuild_jev(payload: Mapping, stage: str):
    """Rebuild the recorded ``JevBatchResult`` so the caller sees the same shape it recorded."""
    from rag.jev.client import JevBatchResult
    from rag.jev.models import PassageJudgment, PreflightDecision

    available = bool(payload.get("available"))
    duration = float(payload.get("duration_ms") or 0.0)
    if not available:
        return JevBatchResult(
            available=False, failure_kind=payload.get("failure_kind"), duration_ms=duration
        )
    raw = payload.get("value") or []
    if stage == "preflight":
        decision = PreflightDecision.model_validate(raw[0]) if raw else None
        return JevBatchResult(value=decision, available=True, duration_ms=duration)
    judgments = tuple(PassageJudgment.model_validate(item) for item in raw)
    return JevBatchResult(value=judgments, available=True, duration_ms=duration)


def install(fixture: ReplayFixture, *, strict: bool = True) -> Callable[[], None]:
    """Point the four seams at ``fixture``; returns the function that puts them back.

    The seams are replaced on the *live* modules, so this is for a replay process: tests use it
    with :meth:`ReplayFixture` contents, and the sandbox's replay uses it with a recorded file.
    ``strict`` is the only sensible setting for evidence — a missing answer raises rather than
    reaching the network — but it stays a parameter so a caller can measure how complete a
    recording is without failing on the first hole.
    """
    import rag.agent as agent_module
    import rag.embedder as embedder_module
    import rag.query_rewriter as rewriter_module
    import rag.reranker as reranker_module
    from rag.jev.client import JevClient

    original_embed = embedder_module.embed
    original_route = agent_module.route_question
    original_rewrite = rewriter_module.rewrite_query
    original_rerank = reranker_module.Reranker._call_rerank_api
    original_preflight = JevClient.preflight
    original_passages = JevClient.judge_passages

    def embed(texts: list[str]) -> list[list[float]]:
        try:
            return fixture.embed(texts)
        except FixtureMiss:
            if strict:
                raise
            return original_embed(texts)

    def route_question(question: str) -> str:
        try:
            return fixture.route(question)
        except FixtureMiss:
            if strict:
                raise
            return original_route(question)

    def rewrite_query(question: str) -> str:
        try:
            return fixture.rewrite(question)
        except FixtureMiss:
            if strict:
                raise
            return original_rewrite(question)

    def call_rerank_api(self, query: str, texts: list[str], top_n: int):
        try:
            return fixture.rerank(query, texts)
        except FixtureMiss:
            if strict:
                raise
            return original_rerank(self, query, texts, top_n)

    def preflight(self, state: dict):
        try:
            return fixture.lookup_preflight(state)
        except FixtureMiss:
            if strict:
                raise
            return original_preflight(self, state)

    def judge_passages(self, state: dict, chunk_ids: list[str]):
        try:
            return fixture.lookup_passages(state, chunk_ids)
        except FixtureMiss:
            if strict:
                raise
            return original_passages(self, state, chunk_ids)

    embedder_module.embed = embed
    agent_module.route_question = route_question
    rewriter_module.rewrite_query = rewrite_query
    reranker_module.Reranker._call_rerank_api = call_rerank_api
    JevClient.preflight = preflight
    JevClient.judge_passages = judge_passages

    def uninstall() -> None:
        embedder_module.embed = original_embed
        agent_module.route_question = original_route
        rewriter_module.rewrite_query = original_rewrite
        reranker_module.Reranker._call_rerank_api = original_rerank
        JevClient.preflight = original_preflight
        JevClient.judge_passages = original_passages

    return uninstall


class RecordingSession:
    """Calls through to the real seams while remembering every answer.

    Recording has to happen where credentials exist, because the whole point is that the replay
    runs without them. The session keeps the answers in memory and the caller writes them once,
    together with the corpus fingerprint and the seam hashes that make the recording verifiable.
    """

    def __init__(self) -> None:
        self.embeddings: dict[str, list[float]] = {}
        self.reranks: dict[str, list] = {}
        self.routes: dict[str, str] = {}
        self.rewrites: dict[str, str] = {}
        self.jev_preflights: dict[str, dict] = {}
        self.jev_passages: dict[str, dict] = {}

    def fixture(self, *, corpus: str, base_commit: str, now: float | None = None) -> ReplayFixture:
        import time as _time

        return ReplayFixture(
            embeddings=dict(self.embeddings),
            reranks=dict(self.reranks),
            routes=dict(self.routes),
            rewrites=dict(self.rewrites),
            jev_preflights=dict(self.jev_preflights),
            jev_passages=dict(self.jev_passages),
            corpus=corpus,
            base_commit=base_commit,
            seams=seam_hashes(),
            created_at=float(_time.time() if now is None else now),
            version=FIXTURE_VERSION,
        )

    def __enter__(self) -> RecordingSession:
        import rag.agent as agent_module
        import rag.embedder as embedder_module
        import rag.query_rewriter as rewriter_module
        import rag.reranker as reranker_module
        from rag.jev.client import JevClient

        self._original = (
            embedder_module.embed,
            agent_module.route_question,
            rewriter_module.rewrite_query,
            reranker_module.Reranker._call_rerank_api,
            JevClient.preflight,
            JevClient.judge_passages,
        )
        session = self

        def embed(texts: list[str]) -> list[list[float]]:
            vectors = session._original[0](texts)
            for text, vector in zip(texts, vectors, strict=False):
                session.embeddings[_key(text)] = list(vector)
            return vectors

        def route_question(question: str) -> str:
            route = session._original[1](question)
            session.routes[normalize(question)] = str(route)
            return route

        def rewrite_query(question: str) -> str:
            rewritten = session._original[2](question)
            session.rewrites[normalize(question)] = str(rewritten)
            return rewritten

        def call_rerank_api(self, query: str, texts: list[str], top_k: int):
            result = session._original[3](self, query, texts, top_k)
            session.reranks[_rerank_key(query, texts)] = json.loads(json.dumps(result))
            return result

        def preflight(self, state: dict):
            result = session._original[4](self, state)
            try:
                session.jev_preflights[jev_key("preflight", state)] = dump_jev(
                    result, stage="preflight"
                )
            except Exception:  # noqa: BLE001 - 录制失败不能影响在线调用
                logger.warning("replay fixture: could not record a Jev preflight")
            return result

        def judge_passages(self, state: dict, chunk_ids: list[str]):
            result = session._original[5](self, state, chunk_ids)
            try:
                session.jev_passages[jev_key("passages", state, chunk_ids)] = dump_jev(
                    result, stage="passages"
                )
            except Exception:  # noqa: BLE001
                logger.warning("replay fixture: could not record Jev passages")
            return result

        embedder_module.embed = embed
        agent_module.route_question = route_question
        rewriter_module.rewrite_query = rewrite_query
        reranker_module.Reranker._call_rerank_api = call_rerank_api
        JevClient.preflight = preflight
        JevClient.judge_passages = judge_passages
        return self

    def __exit__(self, *_exc) -> None:
        import rag.agent as agent_module
        import rag.embedder as embedder_module
        import rag.query_rewriter as rewriter_module
        import rag.reranker as reranker_module
        from rag.jev.client import JevClient

        (
            embedder_module.embed,
            agent_module.route_question,
            rewriter_module.rewrite_query,
            reranker_module.Reranker._call_rerank_api,
            JevClient.preflight,
            JevClient.judge_passages,
        ) = self._original


def install_from_env() -> Callable[[], None] | None:
    """Install the fixture named by :data:`FIXTURE_ENV`, if any.

    Called by the replay entry point rather than at import time, so nothing in the online path can
    ever pick up a recording by accident.
    """
    path = os.environ.get(FIXTURE_ENV, "").strip()
    if not path or not Path(path).is_file():
        return None
    return install(ReplayFixture.load(path))


__all__ = [
    "CORPUS_SCROLL_LIMIT",
    "FIXTURE_ENV",
    "FIXTURE_VERSION",
    "SEAM_MODULES",
    "SEAM_PATHS",
    "FixtureMiss",
    "RecordingSession",
    "ReplayFixture",
    "corpus_fingerprint",
    "install",
    "install_from_env",
    "normalize",
    "seam_hashes",
    "snapshot_corpus_fingerprint",
]
