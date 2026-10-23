from __future__ import annotations


def _jev_active(bindings, owner, effective_kb_id: str) -> bool:
    """Active Jev routing requires mode=active and an allowlisted knowledge base."""
    settings = bindings['settings']
    jev = getattr(owner, "jev", None)
    if jev is None or settings.jev_mode != "active":
        return False
    allowlist = {item.strip() for item in settings.jev_kb_allowlist.split(",") if item.strip()}
    return effective_kb_id in allowlist

def _structured_field_names(bindings, owner, coordinator, kb_id, user) -> tuple[str, ...]:
    """Field display names the judge may be shown, or ``()`` when there is nothing to say.

    Reuses ``build_authorized_catalog`` — the very call ``has_datasets`` makes — instead of adding
    another accessor: everything it returns is already filtered by document permission, so the
    hint cannot widen what a principal may learn. Placeholder names (``Column 3``) are dropped:
    they carry no signal and would eat the budget.
    """
    db = getattr(coordinator, "db", None)
    root = getattr(coordinator, "datasets_root", None)
    if db is None or root is None or not kb_id:
        return ()
    try:
        from rag.spreadsheets.catalog import build_authorized_catalog

        catalog = build_authorized_catalog(db, root, kb_id, user)
    except Exception:  # noqa: BLE001 - 这只是给判定器的附加证据，取不到就退回只有布尔
        return ()
    names: list[str] = []
    seen: set[str] = set()
    for dataset in catalog.datasets:
        for sheet in dataset.sheets:
            for column in sheet.columns:
                name = str(column.display_name or "").strip()
                if not name or name in seen or name.lower().startswith("column "):
                    continue
                seen.add(name)
                names.append(name)
                if len(names) >= owner._STRUCTURED_FIELD_HINT_LIMIT:
                    return tuple(names)
    return tuple(names)

def _submit_shadow_case(bindings, owner, question, query, legacy_route, context, candidates, coordinator, effective_kb_id=None, user=None):
    """Queue a non-blocking shadow comparison; never alters the current response."""
    KbCapabilities = bindings['KbCapabilities']
    ShadowCase = bindings['ShadowCase']
    evaluator = getattr(owner, "_shadow_evaluator", None)
    if evaluator is None:
        return
    has_datasets = getattr(coordinator, "has_datasets", None)
    has_structured = bool(coordinator is not None and (has_datasets(effective_kb_id, user) if has_datasets and effective_kb_id else True))
    evaluator.submit(ShadowCase(
        question=question,
        query=query,
        legacy_route=legacy_route,
        legacy_ranks=tuple(
            (f"{chunk.doc_name}#{chunk.chunk_index}", chunk.rerank_score) for chunk in context
        ),
        candidates=tuple(candidates),
        capabilities=KbCapabilities(
            has_structured_data=has_structured,
            structured_fields=(
                owner._structured_field_names(coordinator, effective_kb_id, user)
                if has_structured
                else ()
            ),
        ),
    ))

def _prepare_context_with_jev(bindings,
    owner, question, session_id, doc_name, top_k, tags, user, kb_id,
    enterprise_request, effective_kb_id, coordinator,
):
    """Jev 主链路：输入 guard 之后先预判路由，再检索与候选判断。"""
    KbCapabilities = bindings['KbCapabilities']
    _TOOL_ROUTE_UNAVAILABLE_COPY = bindings['_TOOL_ROUTE_UNAVAILABLE_COPY']
    _produced_answer = bindings['_produced_answer']
    time = bindings['time']
    has_datasets = getattr(coordinator, "has_datasets", None)
    has_structured = bool(coordinator is not None and (has_datasets(effective_kb_id, user) if has_datasets else True))
    capabilities = KbCapabilities(
        has_structured_data=has_structured,
        structured_fields=(
            owner._structured_field_names(coordinator, effective_kb_id, user)
            if has_structured
            else ()
        ),
    )
    started = time.perf_counter()
    route_decision = owner._route_with_history_correction(
        question, session_id, capabilities
    )
    preflight = route_decision.preflight
    stage = {
        "_started": started,
        "preflight_ms": (time.perf_counter() - started) * 1000,
        "passages_ms": 0.0,
        "route": route_decision.route,
        "reason": route_decision.reason,
        "question_set_version": preflight.question_set_version if preflight is not None else "",
        "passages_question_set_version": "",
        "intent_confidence": preflight.intent_confidence if preflight is not None else None,
        "rewrite_probability": preflight.rewrite_probability if preflight is not None else None,
        "failure_kind": (
            route_decision.reason.split(":", 1)[1]
            if route_decision.reason.startswith("preflight_unavailable:")
            else None
        ),
        "attempts": 0,
        "rewrites": 0,
        # 判定层原始统计量：多轮尝试各取最大值（"判定层最多看到过多少"），
        # 失败的归因据此区分"没召回"与"召回了却没被判为答案证据"。
        "passage_judged": 0,
        "passage_relevant": 0,
        "passage_evidence": 0,
        "passage_contradicted": 0,
        "passage_injected": 0,
    }

    if route_decision.route == "clarification":
        prepared = owner._fixed_decision(
            question,
            effective_kb_id,
            "clarification_required",
            (route_decision.reason,),
        )
        return owner._attach_jev_stage(prepared, stage), None

    structured_clarification = None
    if route_decision.route == "structured":
        structured = owner._attempt_structured_query(
            question, coordinator, user, effective_kb_id, record=stage
        )
        if structured is not None and not owner._is_structured_clarification(structured):
            return owner._attach_jev_stage(structured, stage), None
        if structured is not None:
            # 同上：澄清让位于文档检索，检索拿不到来源时才回澄清。
            structured_clarification = structured
        # 结构化协调器无法处理时退回文档检索

    elif route_decision.route == "tool":
        if enterprise_request:
            # 企业模式继续遵守既有 typed-evidence 限制；文案要说清"需要工具执行"，
            # 不能说成"资料冲突/范围不明确"（A-07）。
            prepared = owner._fixed_decision(
                question,
                effective_kb_id,
                "clarification_required",
                ("jev tool route has no typed evidence",),
                answer=_TOOL_ROUTE_UNAVAILABLE_COPY,
            )
            return owner._attach_jev_stage(prepared, stage), None
        prepared = {"route": "agent", "question": question}
        if user is not None:
            prepared["user"] = user
        if kb_id is not None:
            prepared["kb_id"] = kb_id
        return owner._attach_jev_stage(prepared, stage), None

    elif route_decision.route == "legacy":
        # 预判低置信度或 Jev 不可用：完整降级到基准路由与重排流水线。
        # **降级也留痕**：否则这些请求既没有预判版本也没有判定统计，事后只看到
        # "旧链路作答"，无法解释是谁降级的、为什么，也无法与候选对照
        # （2026-09-30 端到端灰度验证时踩到）。判定阶段确实没跑，故统计量为 0，
        # 由 jev_route="legacy" 与 route_reason 表达降级事实。
        prepare_fn = (
            owner._prepare_context_legacy
            if owner._prepare_context_legacy != owner._prepare_context_standard
            else owner._prepare_context_standard
        )
        prepared, error = prepare_fn(
            question, session_id, doc_name, top_k, tags, user, kb_id,
            enterprise_request, effective_kb_id, coordinator,
            # stage 必须传进去：结构化尝试的结果种类写在它上面，
            # 再由 _attach_jev_stage 搬进 decision_metadata（否则这一列永远是 NULL）。
            stage=stage,
        )
        if prepared is not None:
            prepared = owner._attach_jev_stage(prepared, stage)
        return prepared, error

    prepared, stage = owner._prepare_rag_with_jev(
        question, session_id, doc_name, top_k, tags, user, kb_id,
        enterprise_request, effective_kb_id, route_decision, stage,
    )
    if structured_clarification is not None and not _produced_answer(prepared or {}):
        # 与旧链路同一条判据（见 ``_produced_answer``）：澄清只在检索**没给出答案**时胜出。
        # 拒答带来源是常态，所以不能用"有没有来源"来判定。
        return owner._attach_jev_stage(structured_clarification, stage), None
    return prepared, None

def _prepare_rag_with_jev(bindings,
    owner, question, session_id, doc_name, top_k, tags, user, kb_id,
    enterprise_request, effective_kb_id, route_decision, stage,
):
    """RAG 主链路：原问题检索（除非策略请求改写），Jev 候选判断，最多一次改写重试。"""
    Chunk = bindings['Chunk']
    _RETRIEVAL_OVERSAMPLE = bindings['_RETRIEVAL_OVERSAMPLE']
    _drop_redacted_price_evidence = bindings['_drop_redacted_price_evidence']
    is_self_contained_lookup = bindings['is_self_contained_lookup']
    time = bindings['time']
    retrieve_top_k = top_k * _RETRIEVAL_OVERSAMPLE
    # should_rewrite 与改写重试都是检索路径建议；结构化路由回退到文档检索时两者都不适用。
    retrieval_route = route_decision.route == "rag"
    initial_rewrite = retrieval_route and route_decision.should_rewrite
    # 状态机：attempts=[(检索查询, 是否已改写)]；最多追加一次改写重试，之后必须停止。
    attempts = [
        (owner._rewrite_for_retrieval(question, session_id) if initial_rewrite else question, initial_rewrite)
    ]
    stage["attempts"] = len(attempts)
    stage["rewrites"] = 1 if initial_rewrite else 0
    context: list[Chunk] = []
    conflicts: list[dict] = []
    # 引用兜底：判定层判为"相关"的块即使没有直接答案证据，对用户仍有引用价值。
    # 此前只有"找到答案证据"的那一次尝试才把采纳结果带出去，于是每次尝试都没有答案证据时
    # sources 为空——比旧路径少给了本该给出的来源（2026-10-19 实测 real-025：期望文档被判
    # rel=0.79/证据 0.40，旧路径排第 1，新路径 0 来源、Top-5 丢召回）。**不因此放宽任何
    # 阈值**：这些块只进 sources（在证据包里是 ``role="background"``，可答性/置信度都不
    # 统计它们），不进 context、不当答案证据，答案照旧是"证据不足"。
    # 取采纳最多的一次尝试，与上面判定统计量"跨尝试取最大"的口径一致。
    citation_fallback: list[Chunk] = []
    # 答案证据 / 引用（含背景块）在命中答案证据的那次尝试里定稿（A-03）。
    citations: list[Chunk] | None = None
    background: list[Chunk] = []
    passages_started = time.perf_counter()
    index = 0
    while index < len(attempts):
        query, rewrite_used = attempts[index]
        candidates = owner._retrieve_visible_candidates(
            query,
            top_k=retrieve_top_k,
            doc_name=doc_name,
            tags=tags,
            user=user,
            kb_id=kb_id,
        )
        # ACL 过滤后的候选才允许发送给 Jev；不可见文本不得越过边界。
        selection = owner.jev.select(question, candidates, query=query, top_k=top_k)
        if selection is None:
            # Jev 候选判断不可用：旧 reranker 兜底，来源仍标注为 reranker
            context = owner._post_rank_acl(
                owner._rerank_for_evidence(question, query, candidates, record=stage),
                user,
                effective_kb_id,
            )
            context = owner._drop_injected_chunks(context)[:top_k]
            conflicts = []
            break
        # 价格类问题里，脱敏占位符不算答案证据：保留引用，但不让模型编金额。
        selection = _drop_redacted_price_evidence(question, selection)
        conflicts = [owner._conflict_payload(item) for item in selection.conflicts]
        stage["passages_question_set_version"] = selection.question_set_version
        # 判定层原始统计量（跨尝试取最大）：末次尝试可能因为改写而候选更少，取最大
        # 才是"判定层本可以依据的最强证据"，也才不会把改写后的退化读成召回缺口。
        stage["passage_judged"] = max(stage["passage_judged"], selection.judged)
        stage["passage_relevant"] = max(stage["passage_relevant"], selection.relevant)
        stage["passage_evidence"] = max(stage["passage_evidence"], selection.with_evidence)
        stage["passage_contradicted"] = max(stage["passage_contradicted"], selection.contradicted)
        stage["passage_injected"] = max(stage["passage_injected"], selection.injected)
        if selection.has_answer_evidence:
            # A-03：答案只依据答案证据；判定为"相关但无答案证据"的背景块只进引用。
            visible = owner._drop_injected_chunks(
                owner._post_rank_acl(list(selection.accepted), user, effective_kb_id),
                # 判定层已经给过语义注入结论（并把这些块排除在 accepted 之外），
                # 不再为同一批块付第二次模型调用（A-05）。
                semantic=False,
            )[:top_k]
            evidence_keys = {
                (chunk.doc_name, chunk.chunk_index, chunk.text)
                for chunk in selection.evidence_chunks()
            }
            answer_evidence = [
                chunk
                for chunk in visible
                if (chunk.doc_name, chunk.chunk_index, chunk.text) in evidence_keys
            ]
            if answer_evidence:
                context = answer_evidence
                citations = visible
                background = [
                    chunk
                    for chunk in visible
                    if (chunk.doc_name, chunk.chunk_index, chunk.text) not in evidence_keys
                ]
                break
            # 证据块全被 ACL/文档 guard 摘掉了：按"没有答案证据"处理，继续兜底。
        # 这一次尝试没有答案证据：记下它采纳的相关块做引用兜底（见上面的说明）。
        if len(selection.accepted) > len(citation_fallback):
            citation_fallback = list(selection.accepted)
        if not rewrite_used and retrieval_route and not is_self_contained_lookup(question):
            # 首轮无答案证据：允许一次改写重试；已改写则停止，不再循环。
            attempts.append((owner._rewrite_for_retrieval(question, session_id), True))
            stage["attempts"] = len(attempts)
            stage["rewrites"] = 1
        index += 1
    stage["passages_ms"] = (time.perf_counter() - passages_started) * 1000
    if not context and citation_fallback:
        # ACL 复查与文档注入 guard 在这一层照旧生效：被引用不等于被信任。
        citations = owner._drop_injected_chunks(
            owner._post_rank_acl(citation_fallback, user, effective_kb_id),
            semantic=False,
        )[:top_k]
        # 引用兜底的块是背景：证据包保留 role=background，拒答原因因此能区分
        # "没检索到"和"检索到了、只是没有答案证据"（A-10）。
        background = list(citations)
    prepared = owner._finalize_rag_context(
        question, session_id, context, top_k, effective_kb_id,
        build_bundle=enterprise_request or not context,
        conflicts=tuple(conflicts),
        citations=citations,
        background=background,
    )
    return owner._attach_jev_stage(prepared, stage), stage

def _attach_jev_stage(bindings, prepared: dict, stage: dict) -> dict:
    """Attach sanitized stage timings and decision metadata for the execution trace."""
    settings = bindings['settings']
    time = bindings['time']
    prepared["stage_timings"] = {
        "jev_preflight_ms": stage["preflight_ms"],
        "jev_passages_ms": stage["passages_ms"],
        "preparation_ms": (time.perf_counter() - stage["_started"]) * 1000,
    }
    prepared["decision_metadata"] = {
        "jev_mode": settings.jev_mode,
        "question_set_version": stage["question_set_version"],
        "passages_question_set_version": stage["passages_question_set_version"],
        "jev_route": stage["route"],
        "route_reason": stage["reason"],
        "intent_confidence": stage["intent_confidence"],
        "rewrite_probability": stage["rewrite_probability"],
        "failure_kind": stage["failure_kind"],
        "attempt_count": stage["attempts"],
        "rewrite_count": stage["rewrites"],
        "passage_judged": stage["passage_judged"],
        "passage_relevant": stage["passage_relevant"],
        "passage_evidence": stage["passage_evidence"],
        "passage_contradicted": stage["passage_contradicted"],
        "passage_injected": stage["passage_injected"],
    }
    # 结构化尝试的种类（见 _attempt_structured_query）：只有真的试过才有这个键，
    # 没试过就不写——缺省是"没试"，不是"试了但没结果"。
    if stage.get("structured_query_kind"):
        prepared["decision_metadata"]["structured_query_kind"] = stage["structured_query_kind"]
    if "rewrite_dual_rerank" in stage:
        prepared["decision_metadata"]["rewrite_dual_rerank"] = stage["rewrite_dual_rerank"]
    return prepared
