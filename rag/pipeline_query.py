from __future__ import annotations

from contextlib import aclosing, suppress

from rag.stage_timing import current_stage_timings, measure_call, measure_stream, request_stage_timing_scope

_TERMINAL_STATE_KEY = "_pipeline_terminal_state"


def _claim_terminal(state):
    if state["claimed"]:
        return False
    state["claimed"] = True
    return True


def _trace_stage_timings(prepared):
    timings = dict(prepared.get("stage_timings") or {})
    collector = current_stage_timings()
    if collector is not None:
        timings.update(collector.snapshot())
    return timings


def _seal_trace_stage_timings(prepared):
    timings = dict((prepared or {}).get("stage_timings") or {})
    collector = current_stage_timings()
    if collector is not None:
        timings.update(collector.seal())
    return timings


def _trace_decision_metadata(prepared, outcome):
    metadata = dict(prepared.get("decision_metadata") or {})
    metadata["execution_outcome"] = outcome
    return metadata


@request_stage_timing_scope
def query(bindings, owner, question: str, top_k: int = 8, session_id: str = None, doc_name: str = None, tags: list[str] = None, user: dict = None, kb_id: str = None):
    CircuitOpenError = bindings['CircuitOpenError']
    ExecutionTrace = bindings['ExecutionTrace']
    QueryResult = bindings['QueryResult']
    ToolCall = bindings['ToolCall']
    _HISTORICAL_EVIDENCE_WARNING = bindings['_HISTORICAL_EVIDENCE_WARNING']
    _agent_run_kwargs = bindings['_agent_run_kwargs']
    check_output = bindings['check_output']
    generate = bindings['generate']
    is_degraded_answer = bindings['is_degraded_answer']
    logger = bindings['logger']
    time = bindings['time']
    sid = owner._memory_session_id(session_id, user)
    start_time = time.time()

    try:
        prepared, error = owner._prepare_context(question, sid, doc_name, top_k, tags=tags, user=user, kb_id=kb_id)
    except Exception as error:
        owner._record_stream_failure(question, start_time, None, error)
        raise
    if error:
        trace = ExecutionTrace(
            question=question, route="blocked", answer=error, total_ms=0,
            stage_timings=_trace_stage_timings({}),
            decision_metadata={"execution_outcome": "success"},
        )
        owner.tracker.save(trace)
        owner._submit_failure_observation(trace, None, error)
        return QueryResult(answer=error, context=[], sources=[])

    if prepared["route"] == "cached":
        owner.memory.add_message(sid, "user", question)
        owner.memory.add_message(sid, "assistant", prepared["answer"])
        if owner.memory.should_summarize(sid):
            owner.memory.summarize_old_rounds(sid)
        return QueryResult(answer=prepared["answer"], context=[], sources=prepared["sources"])

    if prepared["route"] in {"fixed", "structured"}:
        answer = prepared["answer"]
        context = prepared["context"]
        sources = prepared["sources"]
        tool_calls = []
    elif prepared["route"] == "agent":
        captured_calls = []
        answer = owner.agent.run(
            prepared["question"],
            tool_calls=captured_calls,
            **_agent_run_kwargs(prepared, pipeline=owner, start_time=start_time),
        )
        context = []
        sources = []
        tool_calls = [ToolCall(**c) for c in captured_calls]
    else:
        try:
            answer = measure_call("generation_ms", generate, prepared["messages"])
        except Exception as error:
            if isinstance(error, CircuitOpenError):
                # 熔断不是答案：留失败轨迹/观测后把可识别失败交给端点，绝不入 memory。
                logger.warning("sync generation degraded because the circuit breaker is open")
            owner._record_stream_failure(question, start_time, prepared, error)
            raise
        decision = prepared.get("decision")
        if decision is not None and decision.kind == "answerable_with_warning":
            answer = _HISTORICAL_EVIDENCE_WARNING + answer
        context = prepared["context"]
        sources = prepared["sources"]
        tool_calls = []

    if is_degraded_answer(answer):
        # 防御旧调用方/旧 provider 仍把降级文案当字符串返回：它同样不是答案。
        error = CircuitOpenError("generator")
        logger.warning("sync generation degraded: the degradation copy was returned as an answer")
        owner._record_stream_failure(question, start_time, prepared, error)
        raise error

    total_ms = (time.time() - start_time) * 1000
    chunk_hashes = prepared.get("chunk_hashes", [])
    trace = ExecutionTrace(
        question=question, route=prepared["route"], answer=answer,
        total_ms=total_ms, tool_calls=tool_calls, chunk_hashes=chunk_hashes,
        stage_timings=_trace_stage_timings(prepared),
        decision_metadata=_trace_decision_metadata(prepared, "success"),
    )
    owner.tracker.save(trace)
    owner._submit_failure_observation(trace, prepared)

    # Gap analysis: check if this is a retrieval gap
    if prepared["route"] == "rag" and chunk_hashes:
        weights = prepared.get("weights", {})
        best_weight = max(weights.values()) if weights else 1.0
        if owner._gap_analyzer.is_gap(best_weight, answer):
            owner._gap_analyzer.record_gap(question, best_weight)

    output_check = check_output(answer)
    if output_check.filtered:
        answer = output_check.text
    owner.memory.add_message(sid, "user", question)
    owner.memory.add_message(sid, "assistant", answer)
    if owner.memory.should_summarize(sid):
        owner.memory.summarize_old_rounds(sid)
    return QueryResult(
        answer=answer,
        context=context,
        sources=sources,
        decision=prepared.get("decision"),
        confidence=prepared.get("confidence"),
    )


@request_stage_timing_scope
async def query_stream(
    bindings, owner,
    question: str,
    top_k: int = 8,
    session_id: str = None,
    doc_name: str = None,
    tags: list[str] = None,
    user: dict = None,
    kb_id: str = None,
    model: str = None,
    models: list[str] = None,
):
    """流式查询，yield SSE 格式的事件字符串。"""
    ExecutionTrace = bindings['ExecutionTrace']
    _json = bindings['_json']
    asyncio = bindings['asyncio']
    time = bindings['time']
    sid = owner._memory_session_id(session_id, user)
    start_time = time.time()
    terminal_state = {"claimed": False}

    try:
        prepared, error = await asyncio.to_thread(
            owner._prepare_context, question, sid, doc_name, top_k, tags=tags, user=user, kb_id=kb_id
        )
    except BaseException as exc:
        # 准备阶段就可能失败（实测：检索到的证据缺版本元数据时 build_evidence_bundle
        # 抛 ValueError）。此前这类请求以 HTTP 200 + error 事件收场，却没有轨迹、没有
        # 观测、没有跳过记录——运营侧连"有请求失败了"都看不到（2026-09-30 踩到）。
        if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
            await _persist_cancelled_trace(bindings, owner, question, start_time, None, terminal_state)
        elif isinstance(exc, Exception) and _claim_terminal(terminal_state):
            await asyncio.to_thread(owner._record_stream_failure, question, start_time, None, exc)
        raise
    if error:
        yield f'data: {_json.dumps({"type": "error", "reason": error}, ensure_ascii=False)}\n\n'
        trace = ExecutionTrace(
            question=question, route="blocked", answer=error, total_ms=0,
            stage_timings=_trace_stage_timings({}),
            decision_metadata={"execution_outcome": "success"},
        )
        _claim_terminal(terminal_state)
        await asyncio.to_thread(owner.tracker.save, trace)
        owner._submit_failure_observation(trace, None, error)
        return

    prepared[_TERMINAL_STATE_KEY] = terminal_state
    stream_iterator = owner._stream_prepared(
        question, prepared, sid, start_time, model=model, models=models
    )
    try:
        async for event in stream_iterator:
            yield event
    except BaseException as exc:
        # 流式中途失败：同样先留痕再分类。已准备好的判定统计量与问题集版本照旧写进
        # 轨迹，所以这次失败仍然可归因。异常继续上抛，端点照旧给客户端一个 error 事件。
        if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
            await _persist_cancelled_trace(bindings, owner, question, start_time, prepared, terminal_state)
        elif isinstance(exc, Exception) and _claim_terminal(terminal_state):
            await asyncio.to_thread(owner._record_stream_failure, question, start_time, prepared, exc)
        raise
    finally:
        close_iterator = getattr(stream_iterator, "aclose", None)
        if close_iterator is not None:
            await close_iterator()


async def _persist_cancelled_trace(bindings, owner, question, start_time, prepared, terminal_state):
    """Persist one frozen cancellation snapshot only while this request has no final trace."""
    if not _claim_terminal(terminal_state):
        return
    ExecutionTrace = bindings['ExecutionTrace']
    time = bindings['time']
    metadata = dict((prepared or {}).get("decision_metadata") or {})
    metadata["execution_outcome"] = "cancelled"
    trace = ExecutionTrace(
        question=question,
        route="error",
        answer="",
        total_ms=(time.time() - start_time) * 1000,
        chunk_hashes=(prepared or {}).get("chunk_hashes") or [],
        stage_timings=_seal_trace_stage_timings(prepared),
        decision_metadata=metadata,
    )
    with suppress(Exception):
        await bindings['asyncio'].to_thread(owner.tracker.save, trace)


async def stream_prepared(
    bindings, owner,
    question,
    prepared,
    sid,
    start_time,
    model: str = None,
    models: list[str] = None,
):
    """一次已准备完毕的请求的流式输出；异常由 :meth:`query_stream` 兜底留痕。"""
    CircuitOpenError = bindings['CircuitOpenError']
    ExecutionTrace = bindings['ExecutionTrace']
    ToolCall = bindings['ToolCall']
    _HISTORICAL_EVIDENCE_WARNING = bindings['_HISTORICAL_EVIDENCE_WARNING']
    _agent_run_kwargs = bindings['_agent_run_kwargs']
    _degraded_sse_event = bindings['_degraded_sse_event']
    _json = bindings['_json']
    _reliability_payload = bindings['_reliability_payload']
    asyncio = bindings['asyncio']
    check_output = bindings['check_output']
    circuit_open_cause = bindings['circuit_open_cause']
    generate_stream = bindings['generate_stream']
    get_llm_pool_router = bindings['get_llm_pool_router']
    is_degraded_answer = bindings['is_degraded_answer']
    logger = bindings['logger']
    time = bindings['time']
    terminal_state = prepared.setdefault(_TERMINAL_STATE_KEY, {"claimed": False})

    async def record_terminal_failure(error):
        if _claim_terminal(terminal_state):
            await asyncio.to_thread(
                owner._record_stream_failure, question, start_time, prepared, error
            )
    # 轨迹 route 的默认值就是路由决策本身。竞技场（多模型并行）只是 RAG 路由之上的
    # 一种执行模式——``prepared["route"]`` 仍是 "rag"，轨迹另记 "arena"，让审计能区分
    # "单模型回答"与"竞技场合并回答"。下游按 route 分支的地方（recorder 的 blocked、
    # clusterer 的 structured 特判，evidence 的 blocked 排除，metrics 完全不读 route）
    # 都不会因为多一个 "arena" 取值算错；evolution 的 route 列是自由文本，聚类键里也
    # 不含 route，所以竞技场观测仍与同名问题的普通 RAG 观测聚在一起。
    trace_route = prepared["route"]
    if prepared["route"] == "cached":
        yield f'data: {_json.dumps({"type": "token", "content": prepared["answer"]}, ensure_ascii=False)}\n\n'
        yield f'data: {_json.dumps({"type": "sources", "sources": prepared["sources"]}, ensure_ascii=False)}\n\n'
        await asyncio.to_thread(owner.memory.add_message, sid, "user", question)
        await asyncio.to_thread(owner.memory.add_message, sid, "assistant", prepared["answer"])
        if await asyncio.to_thread(owner.memory.should_summarize, sid):
            await asyncio.to_thread(owner.memory.summarize_old_rounds, sid)
        _claim_terminal(terminal_state)
        yield 'data: {"type": "done"}\n\n'
        return

    if prepared["route"] in {"fixed", "structured"}:
        answer = prepared["answer"]
        yield f'data: {_json.dumps({"type": "token", "content": answer}, ensure_ascii=False)}\n\n'
        if prepared["route"] == "structured":
            yield f'data: {_json.dumps({"type": "structured_result", **prepared["structured_result"]}, ensure_ascii=False)}\n\n'
        sources = prepared["sources"]
        tool_calls = []
    elif prepared["route"] == "agent":
        captured_calls = []
        answer = await asyncio.to_thread(
            owner.agent.run,
            prepared["question"],
            tool_calls=captured_calls,
            **_agent_run_kwargs(prepared, pipeline=owner, start_time=start_time),
        )
        yield f'data: {_json.dumps({"type": "token", "content": answer}, ensure_ascii=False)}\n\n'
        sources = []
        tool_calls = [ToolCall(**c) for c in captured_calls]
    elif models and len(models) > 1:
        router = get_llm_pool_router()
        arena_targets = [
            router.providers[provider_id]
            for provider_id in models
            if provider_id in router.providers
        ]
        if arena_targets and not any(provider.is_available() for provider in arena_targets):
            # 所有被选中的模型都处于熔断/停用状态：没有模型会产出 token，这不是答案。
            error = CircuitOpenError("llm_pool_arena")
            logger.warning("arena generation degraded: every selected provider is unavailable")
            await record_terminal_failure(error)
            yield _degraded_sse_event()
            return
        model_answers: dict[str, str] = {}
        measured_stream = measure_stream(
            "generation_ms",
            router.generate_stream_parallel(prepared["messages"], provider_ids=models),
        )
        async with aclosing(measured_stream):
            async for event in measured_stream:
                if event.get("type") == "token":
                    m_id = event.get("model", "")
                    model_answers[m_id] = model_answers.get(m_id, "") + event.get("content", "")
                if event.get("type") != "done":
                    yield f'data: {_json.dumps(event, ensure_ascii=False)}\n\n'

        # 汇总各模型回答写入对话记忆与执行跟踪，保证多轮对话与审计观测完整。
        # 合并回答不再在这里自行落库后提前 return，而是与单模型分支共用下面的统一后处理：
        # check_output 过滤后的文本才入记忆/落轨迹，失败也照常交给自演化观测。流式已发出的
        # token 收不回来（见下方后处理处的说明），这一点与单模型分支是同一个固有权衡。
        answer = "\n\n".join(f"### [{m}]\n{ans}" for m, ans in model_answers.items())
        sources = prepared["sources"]
        tool_calls = []
        trace_route = "arena"
    else:
        answer = ""
        decision = prepared.get("decision")
        if decision is not None and decision.kind == "answerable_with_warning":
            answer = _HISTORICAL_EVIDENCE_WARNING
            yield f'data: {_json.dumps({"type": "token", "content": answer}, ensure_ascii=False)}\n\n'

        router = get_llm_pool_router()
        if len(router.providers) > 1 or model:
            try:
                measured_stream = measure_stream(
                    "generation_ms",
                    router.generate_stream_balanced(prepared["messages"], preferred_provider_id=model),
                )
                async with aclosing(measured_stream):
                    async for event in measured_stream:
                        token = event.get("content", "")
                        answer += token
                        yield f'data: {_json.dumps(event, ensure_ascii=False)}\n\n'
            except Exception as error:
                cause = circuit_open_cause(error)
                if cause is None:
                    raise
                logger.warning("stream generation degraded: %s is open", cause.service)
                await record_terminal_failure(cause)
                yield _degraded_sse_event()
                return
        else:
            try:
                measured_stream = measure_stream(
                    "generation_ms", generate_stream(prepared["messages"])
                )
                async with aclosing(measured_stream):
                    async for token in measured_stream:
                        answer += token
                        yield f'data: {_json.dumps({"type": "token", "content": token}, ensure_ascii=False)}\n\n'
            except CircuitOpenError as error:
                logger.warning("stream generation degraded because the generator breaker is open")
                await record_terminal_failure(error)
                yield _degraded_sse_event()
                return
        sources = prepared["sources"]
        tool_calls = []

    if is_degraded_answer(answer):
        # 防御旧 provider 仍把降级文案当 token 流回来：不写 memory、不发 done。
        error = CircuitOpenError("generator")
        logger.warning("stream generation degraded: the degradation copy was streamed as tokens")
        await record_terminal_failure(error)
        yield _degraded_sse_event()
        return

    # 后处理
    # 注意：流式输出时 token 已经逐个发送给客户端，check_output 无法拦截已发送的内容。
    # 这是流式输出的固有权衡：延迟 vs 安全。过滤后的 answer 仅用于 memory/trace 持久化。
    # 生产环境可通过内容审核 API 在客户端侧做二次过滤。
    output_check = check_output(answer)
    if output_check.filtered:
        answer = output_check.text

    yield f'data: {_json.dumps({"type": "sources", "sources": sources}, ensure_ascii=False)}\n\n'

    total_ms = (time.time() - start_time) * 1000
    chunk_hashes = prepared.get("chunk_hashes", [])
    trace = ExecutionTrace(
        question=question, route=trace_route, answer=answer,
        total_ms=total_ms, tool_calls=tool_calls, chunk_hashes=chunk_hashes,
        stage_timings=_trace_stage_timings(prepared),
        decision_metadata=_trace_decision_metadata(prepared, "success"),
    )
    if not _claim_terminal(terminal_state):
        return
    await asyncio.to_thread(owner.tracker.save, trace)
    owner._submit_failure_observation(trace, prepared)

    # Gap analysis for stream
    # 竞技场分支同样落在这里：它的 prepared["route"] 是 "rag"（合并回答仍是对 RAG 证据的
    # 回答），所以低分无证据的竞技场回答也会进入缺口记录，与单模型分支一致。
    if prepared["route"] == "rag" and chunk_hashes:
        def record_gap():
            weights = prepared.get("weights", {})
            best_weight = max(weights.values()) if weights else 1.0
            if owner._gap_analyzer.is_gap(best_weight, answer):
                owner._gap_analyzer.record_gap(question, best_weight)

        await asyncio.to_thread(record_gap)

    await asyncio.to_thread(owner.memory.add_message, sid, "user", question)
    await asyncio.to_thread(owner.memory.add_message, sid, "assistant", answer)
    if await asyncio.to_thread(owner.memory.should_summarize, sid):
        await asyncio.to_thread(owner.memory.summarize_old_rounds, sid)
    done = {"type": "done"}
    reliability = _reliability_payload(prepared)
    if reliability is not None:
        done["reliability"] = reliability
    yield f'data: {_json.dumps(done, ensure_ascii=False)}\n\n'

