from __future__ import annotations

from rag.models import Chunk


def _route_with_history_correction(bindings, owner, question: str, session_id: str, capabilities):
    """路由决策，多轮时按历史修正一次：**只在"它说要澄清、但历史能补全"这一种情况下重新判**。

    2026-10-13 的多轮基准实测：追问本身不含先行词时（"那一步具体是怎么做的？"），路由层只看
    当前这一句，判 `clarification` 就直接返回，7 例只对 1 例；而旧路径无条件改写，把指代补全之后
    照常走 RAG，7/7。修法是让上下文只在这一处生效：

    * 没判澄清（含 legacy/tool/structured/rag）⇒ 原样返回，**零额外调用**；
    * 没有历史、或改写结果与原问法相同 ⇒ 原样返回澄清，**零额外调用**；
    * 有历史且改写确实改变了问法 ⇒ 用补全后的问法**重新判一次**，按新决策继续。

    只多花一次预判（不是两次模型调用），且只花在"本来要拒答"的那些追问上。改写不可用时退回
    原决策 —— 补全失败不该变成路由失败。
    """
    route_decision = owner.jev.route(question, capabilities)
    if getattr(route_decision, "route", "") != "clarification":
        return route_decision
    if not owner._history_is_available(session_id):
        return route_decision
    try:
        completed = owner._rewrite_for_retrieval(question, session_id)
    except Exception:  # noqa: BLE001 - 补全失败就按原决策走，绝不因此改变答复形态
        return route_decision
    completed = str(completed or "").strip()
    if not completed or completed == question.strip():
        return route_decision
    corrected = owner.jev.route(completed, capabilities)
    # 重新判之后仍然要澄清的话，保留**原**决策（原文才是用户问的那句）。
    if getattr(corrected, "route", "") == "clarification":
        return route_decision
    return corrected

def _memory_session_id(bindings, owner, session_id: str | None, user: dict | None) -> str:
    """Principal-scoped key for every DialogueMemory read and write (B-01).

    The API always passes the authenticated user, so a bare ``session_id`` (and
    the default ``"default"``) becomes ``u{user_id}:{session_id}``. Without a
    principal (CLI/offline) the legacy bare key is kept: there is nobody to
    isolate, and changing it would silently forget existing local history.
    """
    _memory_mod = bindings['_memory_mod']
    user_id = user.get("id") if user else None
    return _memory_mod.session_key(user_id, session_id or getattr(owner, "session_id", None))

def _history_is_available(bindings, owner, session_id: str) -> bool:
    """这个会话里有没有可用的历史轮次；读不到就当作没有。"""
    try:
        return bool(owner.memory.get_recent_messages(session_id, n=2))
    except Exception:  # noqa: BLE001 - 记忆读不到时按"没有历史"处理
        return False

def _rewrite_for_retrieval(bindings, owner, question: str, session_id: str) -> str:
    """检索前改写：按配置带上最近几轮对话。

    **默认 0 轮 ⇒ 与既有行为逐字节一致**（`rewrite_query(question)` 原样调用）。
    指代补全只在多轮里才有意义（"它呢？"当轮没有先行词），而回答生成那条路**本来**就带上下文
    （`self.memory.build_messages`）—— 缺口只在检索前的改写这一处。

    改写由 LLM 生成，同一问题多次调用会得到不同句子；实测（2026-10-21）其中一种会把
    "帮我找资料"改写成"如何查询资料"，使同一候选池的重排分从 0.7 掉到 0.03。因此这里
    按 (问题, 历史签名) 缓存改写结果，减少重复抖动与模型调用（见 ``_rewrite_cached``）。
    """
    _history_signature = bindings['_history_signature']
    rewrite_query = bindings['rewrite_query']
    settings = bindings['settings']
    turns = int(getattr(settings, "query_rewrite_history_turns", 0) or 0)
    if turns <= 0:
        return owner._rewrite_cached(question, "", lambda: rewrite_query(question))
    try:
        messages = owner.memory.get_recent_messages(session_id, n=turns * 2)
        summary = owner.memory.get_summary(session_id)
    except Exception:  # noqa: BLE001 - 记忆读不到就退回既有行为，绝不让改写失败
        # 不缓存：这是"没有历史"的退化结果，缓存它会让 memory 恢复后仍返回无上下文改写。
        return rewrite_query(question)
    history = [(m.role, m.content) for m in messages]
    return owner._rewrite_cached(
        question,
        _history_signature(history, summary),
        lambda: rewrite_query(question, history=history, summary=summary),
    )

def _rewrite_cached(bindings, owner, question: str, signature: str, produce) -> str:
    """Return a cached rewrite or produce and cache a new one.

    Only a non-empty rewrite that actually **changed** the question is cached. ``rewrite_query``
    returns the original question when the model call fails, and freezing that would turn a
    transient provider error into a permanent loss of recall for that question.
    """
    OrderedDict = bindings['OrderedDict']
    is_degraded_answer = bindings['is_degraded_answer']
    settings = bindings['settings']
    threading = bindings['threading']
    if not getattr(settings, "query_rewrite_cache_enabled", True):
        return produce()
    cache = getattr(owner, "_rewrite_cache", None)
    lock = getattr(owner, "_rewrite_cache_lock", None)
    if cache is None or lock is None:
        # Tests build pipelines with object.__new__ and skip __init__; create lazily.
        cache = OrderedDict()
        lock = threading.Lock()
        owner._rewrite_cache = cache
        owner._rewrite_cache_lock = lock
    key = (question, signature)
    with lock:
        cached = cache.get(key)
        if cached is not None:
            cache.move_to_end(key)
            # 旧版本可能已经把降级文案写进缓存（P1-01）：命中它也不能当改写结果。
            return question if is_degraded_answer(cached) else cached
    rewritten = produce()
    if is_degraded_answer(rewritten):
        # A degradation copy is never a valid rewrite: using it as the retrieval
        # query would permanently pin this question to garbage recall (P1-01).
        return question
    if not rewritten or rewritten == question:
        return rewritten
    with lock:
        cache[key] = rewritten
        cache.move_to_end(key)
        limit = max(1, int(getattr(settings, "query_rewrite_cache_size", 512) or 512))
        while len(cache) > limit:
            cache.popitem(last=False)
    return rewritten

def _rerank_for_evidence(bindings, owner, original_question, retrieval_query, candidates, record=None):
    """Rerank with the retrieval query, and also with the user's original wording.

    Measured 2026-10-21: an LLM rewrite can turn "find this for me" into "how do I query for
    this"; the reranker then scores the same relevant document near zero and the request flips
    to a refusal. Judging both phrasings and keeping the higher score removes the flip without
    touching the evidence threshold.

    The second pass is deliberately conditional: with ``below_threshold`` (default) it only runs
    when the rewrite pass is already weak enough to risk a refusal, so the common request keeps
    its single-rerank latency. A degraded reranker (all scores ``None``) and an unchanged query
    never trigger it.
    """
    merge_ranked_chunks = bindings['merge_ranked_chunks']
    settings = bindings['settings']
    if not candidates:
        return []
    primary = owner.reranker.rerank(retrieval_query, candidates)
    if record is not None:
        record["rewrite_dual_rerank"] = False
    if retrieval_query.strip() == original_question.strip():
        return primary
    if not any(chunk.rerank_score is not None for chunk in primary):
        # 重排器降级（断路器/接口失败）：再调一次只会同样失败。
        return primary
    mode = settings.rewrite_dual_rerank_mode
    if mode == "off":
        return primary
    if mode == "below_threshold":
        best = max(chunk.rerank_score for chunk in primary if chunk.rerank_score is not None)
        if best >= settings.rewrite_dual_rerank_threshold:
            return primary
    secondary = owner.reranker.rerank(original_question, candidates)
    if record is not None:
        record["rewrite_dual_rerank"] = True
    return merge_ranked_chunks(primary, secondary) or primary

def _prepare_context(bindings, owner, question: str, session_id: str, doc_name: str, top_k: int = 8, tags: list[str] = None, user: dict = None, kb_id: str = None):
    """公共逻辑：guard -> route -> retrieve -> judge -> build_messages。"""
    assess_confidence = bindings['assess_confidence']
    build_evidence_bundle = bindings['build_evidence_bundle']
    check_injection = bindings['check_injection']
    decide_answerability = bindings['decide_answerability']
    fixed_decision_copy = bindings['fixed_decision_copy']
    sanitize_input = bindings['sanitize_input']
    settings = bindings['settings']
    # 任何进入 pipeline 的记忆读写都先落到主体命名空间（幂等，见 session_key）。
    session_id = owner._memory_session_id(session_id, user)
    question = sanitize_input(question)
    guard = check_injection(question)
    if guard.blocked:
        return None, f"请求被安全策略拦截：{guard.reason}"

    enterprise_request = getattr(owner, "_enterprise_mode", False) or kb_id is not None
    effective_kb_id = kb_id or getattr(owner, "_kb_id", "rag_docs")
    if enterprise_request and (user is None or owner._user_db is None):
        bundle = build_evidence_bundle(
            question,
            [],
            kb_id=effective_kb_id,
            acl_checked=True,
            permission_limited=True,
        )
        decision = decide_answerability(bundle)
        confidence = assess_confidence(bundle, decision)
        return {
            "route": "fixed",
            "answer": fixed_decision_copy(decision.kind),
            "context": [],
            "sources": [],
            "decision": decision,
            "confidence": confidence,
            "evidence_bundle": bundle,
        }, None

    coordinator = getattr(owner, "_spreadsheet_coordinator", None)

    cache_key_kb = effective_kb_id
    cached = None  # Final-answer caching is disabled until ACL versioning is end-to-end.
    if cached:
        # 缓存命中时也要做权限过滤：sources 可能引用用户无权查看的文档
        sources = cached.get("sources") or [{"doc_name": "缓存", "chunk_index": 0}]
        if user is not None and owner._user_db is not None:
            sources = owner._filter_sources_by_permission(sources, cache_key_kb, user)
        return {"route": "cached", "answer": cached.get("answer", ""), "context": [], "sources": sources}, None

    if owner._jev_active(effective_kb_id):
        rollout = getattr(owner, "_question_set_rollout", None)
        if rollout is not None:
            # 层 4 灰度键：同一请求内所有 Jev 判定共用，保证 preflight 与 passages
            # 的候选/当前问题集选择一致；键只存在于进程内，不落库、不记日志。
            with rollout.request_key(f"{effective_kb_id}|{session_id}|{question}"):
                return owner._prepare_context_with_jev(
                    question, session_id, doc_name, top_k, tags, user, kb_id,
                    enterprise_request, effective_kb_id, coordinator,
                )
        return owner._prepare_context_with_jev(
            question, session_id, doc_name, top_k, tags, user, kb_id,
            enterprise_request, effective_kb_id, coordinator,
        )
    prepare_fn = (
        owner._prepare_context_legacy
        if owner._prepare_context_legacy != owner._prepare_context_standard
        else owner._prepare_context_standard
    )
    prepared, error = prepare_fn(
        question, session_id, doc_name, top_k, tags, user, kb_id,
        enterprise_request, effective_kb_id, coordinator,
    )
    if error is None and settings.jev_mode != "off":
        prepared.setdefault("stage_timings", {})
        prepared.setdefault("decision_metadata", {}).setdefault("jev_mode", settings.jev_mode)
    return prepared, error

def _prepare_context_standard(bindings,
    owner, question, session_id, doc_name, top_k, tags, user, kb_id,
    enterprise_request, effective_kb_id, coordinator, stage=None,
):
    """基准检索流水线 (Classic RAG)：结构化优先 -> 意图路由 -> 改写 -> 召回 -> 重排 -> 安全/ACL过滤 -> 上下文定稿。"""
    _RETRIEVAL_OVERSAMPLE = bindings['_RETRIEVAL_OVERSAMPLE']
    _agent_mod = bindings['_agent_mod']
    _produced_answer = bindings['_produced_answer']
    is_self_contained_lookup = bindings['is_self_contained_lookup']
    record = stage if stage is not None else {}
    structured = owner._attempt_structured_query(
        question, coordinator, user, effective_kb_id, record=record
    )
    structured_clarification = None
    if structured is not None:
        if not owner._is_structured_clarification(structured):
            return structured, None
        # 澄清不抢检索（2026-10-19）：问句里出现字段名并不等于表格里就有可直接算出的东西。
        # "r50相关价格" 会被判成结构化，但规划器只会回一句"请指明字段"，而文档里其实有价格。
        # 所以先让它走完检索，拿不到来源时再回澄清——顺序反了就是把能答的问题变成反问。
        structured_clarification = structured

    # 企业模式里 agent 路由只有"固定拒答"一个结局（typed evidence 限制），先问一次路由
    # 模型再拒答等于每条企业问答白付一次模型调用，还放大尾延迟（A-07）。直接走 RAG。
    route = "rag" if enterprise_request else _agent_mod.route_question(question)

    if route == "agent":
        prepared = {"route": "agent", "question": question}
        # 透传当前用户/知识库，供 agent 的 retrieve 工具做权限过滤
        if user is not None:
            prepared["user"] = user
        if kb_id is not None:
            prepared["kb_id"] = kb_id
        return prepared, None

    rewritten = (
        question
        if stage is not None and is_self_contained_lookup(question)
        else owner._rewrite_for_retrieval(question, session_id)
    )
    candidates = owner._retrieve_visible_candidates(
        rewritten,
        top_k=top_k * _RETRIEVAL_OVERSAMPLE,
        doc_name=doc_name,
        tags=tags,
        user=user,
        kb_id=kb_id,
    )
    context = owner._post_rank_acl(
        owner._rerank_for_evidence(question, rewritten, candidates, record=record),
        user,
        effective_kb_id,
    )
    context = owner._drop_injected_chunks(context)[:top_k]
    owner._submit_shadow_case(question, rewritten, "rag", context, candidates, coordinator, effective_kb_id, user)
    prepared = owner._finalize_rag_context(
        question, session_id, context, top_k, effective_kb_id,
        build_bundle=enterprise_request or not context,
    )
    prepared.setdefault("decision_metadata", {})["legacy_route"] = route
    # 结构化尝试的种类跟着这次请求走：它解释"为什么结构化没有帮上忙"，
    # 也是 details_structured_query_kind 唯一的来源（见 _attempt_structured_query）。
    if "structured_query_kind" in record:
        prepared["decision_metadata"]["structured_query_kind"] = record["structured_query_kind"]
    if "rewrite_dual_rerank" in record:
        prepared["decision_metadata"]["rewrite_dual_rerank"] = record["rewrite_dual_rerank"]
    if structured_clarification is not None and not _produced_answer(prepared):
        # 检索没有给出**答案**，才把结构化澄清交给用户（此时它比"资料不足"更有用）。
        # 判据不是"有没有来源"：Jev 路径的拒答本来就带来源（引用兜底），
        # 于是"有来源"曾被当成"检索赢了"，把澄清丢掉、只回一句"现有可见资料不足"。
        return structured_clarification, None
    return prepared, None

def _retrieve_visible_candidates(bindings,
    owner,
    query: str,
    *,
    top_k: int,
    doc_name: str | None,
    tags: list[str] | None,
    user: dict | None,
    kb_id: str,
) -> list[Chunk]:
    """Retrieve, version-scope, and ACL-filter candidates before any external judge."""
    _permissions_mod = bindings['_permissions_mod']
    _versioned_retrieval_mod = bindings['_versioned_retrieval_mod']
    effective_kb_id = kb_id or owner._kb_id
    feedback_weight_cache: dict[str, float] = {}

    def feedback_weight(chunk_hash: str) -> float:
        if chunk_hash not in feedback_weight_cache:
            weight = owner._feedback_processor.get_weight(chunk_hash)
            feedback_weight_cache[chunk_hash] = 1.0 if weight is None else weight
        return feedback_weight_cache[chunk_hash]

    if user is not None and owner._user_db is not None:
        raw_context = _versioned_retrieval_mod.retrieve_current_then_history(
            owner.retriever,
            query,
            top_k=top_k,
            user=user,
            user_db=owner._user_db,
            kb_id=effective_kb_id,
            doc_name=doc_name,
            tags=tags,
            weights=feedback_weight,
        )
    else:
        raw_context = owner.retriever.retrieve(
            query,
            top_k=top_k,
            doc_name=doc_name,
            tags=tags,
            weights=feedback_weight,
        )
    # ACL must run before any external judge so unauthorized text never crosses that boundary.
    if user is not None and owner._user_db is not None:
        raw_context = _permissions_mod.filter_chunks_by_permission(
            owner._user_db, effective_kb_id, raw_context, user
        )
    return raw_context

def _post_rank_acl(bindings, owner, context: list[Chunk], user: dict | None, effective_kb_id: str) -> list[Chunk]:
    """Preserve the post-rank ACL filter as defense in depth."""
    _permissions_mod = bindings['_permissions_mod']
    if user is not None and owner._user_db is not None:
        return _permissions_mod.filter_chunks_by_permission(
            owner._user_db, effective_kb_id, context, user
        )
    return context

def _drop_injected_chunks(bindings, owner, context: list[Chunk], *, semantic: bool = True) -> list[Chunk]:
    """Remove chunks blocked by the injection guards.

    ``semantic`` runs the optional model-side check on top of the deterministic one. The Jev
    route passes ``semantic=False`` for judged passages: its judge already produced the
    ``contains_prompt_injection`` verdict for those chunks, and judging them twice would pay
    for the same call twice. Every other path (legacy reranker, Jev fallback) opts in, so the
    semantic layer no longer depends on which route served the request (A-05).
    """
    check_document_injection = bindings['check_document_injection']
    logger = bindings['logger']
    safe_context = []
    for candidate in context:
        document_guard = check_document_injection(candidate.text)
        if document_guard.blocked:
            logger.warning(
                "检索文档注入命中并排除: doc=%s chunk=%s reason=%s",
                candidate.doc_name,
                candidate.chunk_index,
                document_guard.reason,
            )
            continue
        safe_context.append(candidate)
    if not semantic or not safe_context:
        return safe_context
    return owner._drop_semantically_injected(safe_context)

def _drop_semantically_injected(bindings, owner, context: list[Chunk]) -> list[Chunk]:
    """Apply the optional semantic injection detector; fail-open when it is unavailable."""
    apply_semantic_injection_check = bindings['apply_semantic_injection_check']
    detector = getattr(owner, "_semantic_injection_detector", None)
    if detector is None:
        return context
    result = apply_semantic_injection_check([chunk.text for chunk in context], detector)
    if not result.checked:
        return context
    flagged = set(result.flagged)
    return [chunk for index, chunk in enumerate(context) if index not in flagged]

def _finalize_rag_context(bindings,
    owner, question, session_id, context, top_k, effective_kb_id, *, build_bundle, conflicts=(),
    citations=None, background=(),
):
    """Build messages, sources, feedback weights, and the optional reliability gate.

    ``citations`` names the passages to list as sources when they differ from the answering
    context: the Jev path cites the passages its judge found relevant even when none of them
    carried answer evidence and the request is therefore refused. A refusal may not hide the
    documents that were looked at — the legacy path lists them as sources too, so dropping them
    made the Jev path lose Top-5 recall (2026-10-19, real-025). Citations are **sources only**:
    messages, feedback hashes and the evidence bundle keep using ``context``, so the answer
    itself is still built from (and refused for the lack of) answer evidence. ``None`` keeps the
    1:1 context→sources mapping every other caller relies on.

    ``background`` names the citation-only passages that must still reach the evidence bundle
    as ``role="background"`` refs (A-03): they are visible candidates for metrics and reasons,
    but they never count as answer evidence.
    """
    assess_confidence = bindings['assess_confidence']
    build_evidence_bundle = bindings['build_evidence_bundle']
    decide_answerability = bindings['decide_answerability']
    fixed_decision_copy = bindings['fixed_decision_copy']
    hashlib = bindings['hashlib']
    logger = bindings['logger']
    if build_bundle and context:
        # 版本边界：版本化 bundle 只能引用带 document_id/document_version_id 的块。
        # BM25 降级快照的块没有这些字段（密集检索抖动时会走到那条路），此前它们会让
        # build_evidence_bundle 抛 ValueError("versioned evidence required")，整条请求
        # 以 HTTP 200 + error 事件收场（2026-09-30 实测 3/4 概率）。这里把不可引用的块
        # 摘掉：答案、引用与哈希都只基于可引用的块；全不可引用时自然落到"证据不足"。
        # 检索层保持原样——BM25 快照没有版本元数据是它的正当形态，不该在检索层被禁掉。
        citable = [
            chunk
            for chunk in context
            if chunk.document_id is not None and chunk.document_version_id is not None
        ]
        if len(citable) != len(context):
            logger.warning(
                "dropped %d chunk(s) without version metadata before the evidence bundle",
                len(context) - len(citable),
            )
            context = citable
    background_chunks = [
        chunk
        for chunk in background
        if chunk.document_id is not None and chunk.document_version_id is not None
    ] if build_bundle else []
    chunk_hashes = [hashlib.md5(c.text.encode()).hexdigest() for c in context]
    weights = owner._feedback_processor.get_weights(chunk_hashes)

    sid = session_id or owner.session_id or "default"
    messages = owner.memory.build_messages(sid, question, context)
    cited = list(context) if citations is None else list(citations)
    sources = [
        {
            "doc_name": c.doc_name,
            "chunk_index": c.chunk_index,
            "text_preview": c.text[:100],
            "document_id": c.document_id,
            "document_version_id": c.document_version_id,
            "section_title": c.section_title,
            "is_historical": c.historical_weight < 1.0,
            "rank": i,
        }
        for i, c in enumerate(cited)
    ]
    prepared = {
        "route": "rag", "question": question, "context": context,
        "messages": messages, "sources": sources, "sid": sid,
        "chunk_hashes": chunk_hashes, "weights": weights,
    }
    if build_bundle:
        bundle = build_evidence_bundle(
            question,
            context,
            kb_id=effective_kb_id,
            acl_checked=True,
            conflicts=[dict(item) for item in conflicts],
            background=background_chunks,
        )
        decision = decide_answerability(bundle)
        confidence = assess_confidence(bundle, decision)
        prepared["decision"] = decision
        prepared["confidence"] = confidence
        prepared["evidence_bundle"] = bundle
        if decision.kind in {
            "clarification_required",
            "insufficient_evidence",
            "permission_limited",
        }:
            prepared["route"] = "fixed"
            prepared["answer"] = fixed_decision_copy(decision.kind)
    return prepared

def _fixed_decision(bindings, owner, question, effective_kb_id, kind, reasons, *, permission_limited=False, answer=None):
    """Build a fixed refusal without any external call.

    ``answer`` overrides the kind's generic copy when the decision has a specific, more
    accurate explanation (e.g. "this question needs tool execution", A-07).
    """
    AnswerabilityDecision = bindings['AnswerabilityDecision']
    assess_confidence = bindings['assess_confidence']
    build_evidence_bundle = bindings['build_evidence_bundle']
    fixed_decision_copy = bindings['fixed_decision_copy']
    bundle = build_evidence_bundle(
        question,
        [],
        kb_id=effective_kb_id,
        acl_checked=True,
        permission_limited=permission_limited,
    )
    decision = AnswerabilityDecision(kind=kind, reasons=tuple(reasons))
    confidence = assess_confidence(bundle, decision)
    return {
        "route": "fixed",
        "answer": answer if answer is not None else fixed_decision_copy(decision.kind),
        "context": [],
        "sources": [],
        "decision": decision,
        "confidence": confidence,
        "evidence_bundle": bundle,
    }

def _is_structured_clarification(bindings, prepared: dict | None) -> bool:
    """True when the structured attempt produced only a clarifying question.

    Such an outcome must not preempt retrieval: it asks the user for a field while the documents
    may already hold the answer (measured 2026-10-19 on ``r50相关价格``-style questions).
    """
    if (prepared or {}).get("route") != "structured":
        return False
    result = (prepared or {}).get("structured_result") or {}
    return result.get("kind") == "clarification_required"

def _attempt_structured_query(bindings, owner, question, coordinator, user, effective_kb_id, record=None):
    """Run the deterministic spreadsheet coordinator; return prepared route or None.

    ``record`` (optional) receives ``{"structured_query_kind": <outcome kind>}`` for **every**
    attempt, including the ones that return ``None``. Without it the kind is unrecoverable:
    ``not_applicable`` (coordinator declined), ``unsupported`` (planner could not build a plan)
    and ``failed`` (internal execution error) all collapsed into the same ``None``, so
    "structured execution broke" was indistinguishable from "there is no table data" — and
    ``details_structured_query_kind`` never got written at all.
    """
    if coordinator is None or user is None or effective_kb_id == "rag_docs":
        return None
    has_datasets = getattr(coordinator, "has_datasets", None)
    if has_datasets is not None and not has_datasets(effective_kb_id, user):
        return None
    outcome = coordinator.query(question, kb_id=effective_kb_id, user=user)
    if record is not None:
        record["structured_query_kind"] = outcome.kind
    if outcome.kind in {"structured_result", "hybrid_result"}:
        answer = "\n".join(f"{fact.label}：{fact.value}" for fact in outcome.computed_facts)
        payload = {
            "kind": outcome.kind,
            "facts": [{"label": fact.label, "value": fact.value} for fact in outcome.computed_facts],
            "sources": [
                {"dataset_id": source.dataset_id, "sheet_key": source.sheet_key, "cell_range": source.cell_range}
                for source in outcome.citations
            ],
            "rows_scanned": outcome.rows_scanned,
        }
        return {"route": "structured", "answer": answer, "context": [], "sources": [], "structured_result": payload}
    if outcome.kind == "clarification_required":
        question_text = outcome.clarification.question if outcome.clarification else "请补充表格查询条件"
        return {"route": "structured", "answer": question_text, "context": [], "sources": [], "structured_result": {"kind": "clarification_required", "facts": [], "sources": [], "rows_scanned": 0, "clarification": outcome.clarification.model_dump() if outcome.clarification else None}}
    return None

def _conflict_payload(bindings, chunk: Chunk) -> dict:
    return {
        "doc_name": chunk.doc_name,
        "chunk_index": chunk.chunk_index,
        "text_preview": chunk.text[:100],
        "score_source": chunk.score_source,
    }

def _filter_sources_by_permission(bindings, owner, sources: list, kb_id: str, user: dict) -> list:
    """过滤缓存命中的 sources（dict 而非 Chunk 对象）中用户无权查看的文档来源。

    将每个 source 包装成仅含 doc_name 的轻量对象，复用
    filter_chunks_by_permission 的同一套权限逻辑：admin 不过滤；普通用户
    仅保留显式授权的文档；没有权限记录的文档默认拒绝。
    """
    SimpleNamespace = bindings['SimpleNamespace']
    _permissions_mod = bindings['_permissions_mod']
    if not sources or owner._user_db is None:
        return sources

    proxies = [SimpleNamespace(doc_name=s.get("doc_name")) for s in sources]
    filtered = _permissions_mod.filter_chunks_by_permission(owner._user_db, kb_id, proxies, user)
    allowed_names = {p.doc_name for p in filtered}
    return [s for s in sources if s.get("doc_name") in allowed_names]
