"""Agent module -- LangChain Agent + router."""

import json as _json
import logging as _logging
import os as _os
import re as _re
import sqlite3 as _sqlite3
import threading as _threading
import time as _time

from rag.generator import deepseek_default_headers, generate
from rag.prompt_manager import PromptManager
from rag.upstream_observability import attach_openai_observer

_pm = PromptManager()

logger = _logging.getLogger(__name__)


def resolve_sql_source(db_path: str | None) -> str | None:
    """The SQLite file the agent may query, or ``None`` when there is no usable source.

    A-07：``:memory:`` 从来不是数据源——每条 SQLite 连接都会打开一个新的空库，
    绑上去的工具只能永远返回"查询结果为空"，却让模型以为手上有数据可查。只有**显式给出**
    且**确实存在**的数据库文件才算数据源；否则不注册该工具，模型的工具清单与它真实的
    能力保持一致。
    """
    if not db_path:
        return None
    candidate = str(db_path).strip()
    if not candidate or candidate == ":memory:":
        return None
    if not _os.path.isfile(candidate):
        logger.info("sql tool disabled: data source does not exist: %s", candidate)
        return None
    try:
        # 只验证它是可打开的 SQLite 文件；不要在这里建表/写数据。
        with _sqlite3.connect(f"file:{candidate}?mode=ro", uri=True) as conn:
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except _sqlite3.Error:
        logger.warning("sql tool disabled: not a readable SQLite database: %s", candidate)
        return None
    return candidate


def create_agent_tools(retriever, db_path: str | None = None, output_dir: str = "data/charts", permission_filter=None) -> list:
    """Create the list of tools available to the Agent.

    ``db_path`` names the SQLite file the ``sql_query`` tool may read. Without a usable file the
    tool is **not registered** rather than being bound to an empty ``:memory:`` database (A-07).

    ``permission_filter`` is an optional callable ``callable(list[Chunk]) -> list[Chunk]``
    applied to the retrieval results before formatting. It is used by ``RAGAgent``
    to drop chunks the current user is not allowed to see.
    """
    from langchain_core.tools import Tool

    from rag.tools import calculate, create_sql_tool

    def _retrieve(q: str) -> str:
        """检索知识库并返回格式化片段，已应用权限过滤。"""
        if not retriever:
            return "无检索器"
        results = retriever.retrieve(q, top_k=10)
        if permission_filter is not None:
            results = permission_filter(results)
        return "\n".join(
            f"[{i + 1}] {c.doc_name}(第{c.chunk_index + 1}段): {c.text}"
            for i, c in enumerate(results)
        )

    retrieve_tool = Tool(
        name="retrieve",
        description="从知识库中检索相关文档。输入为查询文本。返回最相关的文档片段。",
        func=_retrieve,
    )
    calc_tool = Tool(
        name="calculate",
        description="安全计算数值表达式。输入为数学表达式，如 '(1200 - 1068) / 1068 * 100'。",
        func=calculate,
    )
    chart_tool = Tool(
        name="plot_chart",
        description="生成图表。输入格式为 '图表类型|标题|标签逗号分隔|数值逗号分隔'，如 'bar|销量对比|A,B,C|100,200,150'。",
        func=lambda inp: _parse_and_plot(inp, output_dir),
    )
    tools = [retrieve_tool, calc_tool]
    sql_source = resolve_sql_source(db_path)
    if sql_source is not None:
        tools.append(
            Tool(
                name="sql_query",
                description="对已导入的数据集执行 SQL 查询。输入为 SELECT 语句。",
                func=create_sql_tool(sql_source),
            )
        )
    else:
        logger.info("agent sql tool not registered: no explicit SQLite data source")
    tools.append(chart_tool)
    return tools


def _parse_and_plot(inp: str, output_dir: str) -> str:
    """Parse chart parameters and generate a chart."""
    from rag.tools import plot_chart

    # 处理 JSON 格式输入（DeepSeek 可能发送 JSON 而非管道分隔字符串）
    if inp.strip().startswith("{"):
        try:
            data = _json.loads(inp)
            chart_type = data.get("type", data.get("chart_type", "bar"))
            title = data.get("title", "")
            labels = data.get("labels", [])
            values = data.get("values", [])
            if isinstance(labels, str):
                labels = [s.strip() for s in labels.split(",")]
            if isinstance(values, str):
                values = [float(s.strip()) for s in values.split(",")]
            if not labels or not values:
                return "缺少标签或数值，请提供完整的 labels 和 values"
            return plot_chart({"labels": labels, "values": values}, chart_type, title, output_dir)
        except (_json.JSONDecodeError, KeyError, ValueError):
            return "JSON 格式错误，请使用: 图表类型|标题|标签逗号分隔|数值逗号分隔"

    parts = inp.split("|")
    if len(parts) != 4:
        return "格式错误，请使用: 图表类型|标题|标签逗号分隔|数值逗号分隔"
    chart_type, title, labels_str, values_str = parts
    labels = [s.strip() for s in labels_str.split(",")]
    try:
        values = [float(s.strip()) for s in values_str.split(",")]
    except ValueError:
        return f"数值格式错误，请确保所有值为数字，收到: {values_str}"
    if len(labels) != len(values):
        return f"标签数量({len(labels)})与数值数量({len(values)})不匹配"
    return plot_chart({"labels": labels, "values": values}, chart_type.strip(), title.strip(), output_dir)


def _extract_chart_paths(messages: list, output_dir: str) -> list[str]:
    """从 agent 消息历史中提取所有图表文件路径。"""
    paths = []
    pattern = _re.compile(r"(?:data[/\\])?chart_\w+\.png")
    for msg in messages:
        content = msg.content if hasattr(msg, "content") else ""
        if not isinstance(content, str):
            content = str(content) if content else ""
        for match in pattern.finditer(content):
            path = match.group(0)
            if "/" not in path and "\\" not in path:
                path = f"{output_dir}/{path}"
            paths.append(path)
        # 也检查 tool_calls 的输出
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                if isinstance(tc, dict):
                    tc_output = str(tc.get("output", ""))
                elif hasattr(tc, "content"):
                    tc_output = str(tc.content)
                else:
                    tc_output = str(tc)
                for match in pattern.finditer(tc_output):
                    path = match.group(0)
                    if "/" not in path and "\\" not in path:
                        path = f"{output_dir}/{path}"
                    paths.append(path)
    return paths


class RAGAgent:
    """Wraps a LangChain Agent with tools."""

    def __init__(self, retriever, db_path: str | None = None, max_iterations: int = 5, output_dir: str = "data/charts", user: dict = None, kb_id: str = "rag_docs", user_db=None):
        self.max_iterations = max_iterations
        self.output_dir = output_dir
        self.user = user
        self.kb_id = kb_id or "rag_docs"
        self.user_db = user_db
        # 只有显式配置的、真实存在的 SQLite 文件才会让 sql_query 工具存在（A-07）。
        # ``:memory:``/缺省都表示"没有可查的数据来源"，工具清单里就不会出现它。
        self.db_path = resolve_sql_source(db_path)
        # per-invocation 权限上下文：run 时写入线程局部，避免写进共享实例
        self._run_ctx = _threading.local()
        self.tools = [
            _wrap_tool_with_reflection(t, recorder=self._record_tool_call)
            for t in create_agent_tools(retriever, self.db_path, output_dir, permission_filter=self._make_permission_filter())
        ]
        self.llm = None
        self.agent = None

    def _ensure_agent(self) -> None:
        """Create the external model client only when an Agent query is requested."""
        if self.agent is not None:
            return

        from langchain.agents import create_agent
        from langchain_openai import ChatOpenAI

        from config import settings

        self.llm = ChatOpenAI(
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            temperature=0.3,
            extra_body={"thinking": {"type": "disabled"}},
            default_headers=deepseek_default_headers(),
        )
        attach_openai_observer(getattr(self.llm, "root_client", None))
        attach_openai_observer(getattr(self.llm, "root_async_client", None), asynchronous=True)

        system_prompt = _pm.get("agent_system")
        self.agent = create_agent(
            model=self.llm,
            tools=self.tools,
            system_prompt=system_prompt,
        )

    def _make_permission_filter(self):
        """构造 retrieve 工具结果的权限过滤回调。

        权限上下文按 invocation 从线程局部（``self._run_ctx``）读取；
        未调用 run 直接使用工具（如单测）时回退到构造参数 ``self.user``/``self.kb_id``。
        - user 或 user_db 缺省时不过滤（向后兼容）
        - 否则复用 pipeline 的同一套 ``filter_chunks_by_permission`` 逻辑
        """

        def filter_fn(chunks: list) -> list:
            user = getattr(self._run_ctx, "user", None)
            if user is None:
                user = self.user
            kb_id = getattr(self._run_ctx, "kb_id", None)
            if kb_id is None:
                kb_id = self.kb_id
            if user is None or self.user_db is None:
                return chunks
            from rag.permissions import filter_chunks_by_permission

            return filter_chunks_by_permission(self.user_db, kb_id, chunks, user)

        return filter_fn

    def _record_tool_call(self, tool_name: str, inp, output, duration_ms: float) -> None:
        """Record a tool call into the current invocation's private list."""
        captured_calls = getattr(self._run_ctx, "tool_calls", None)
        if captured_calls is not None:
            captured_calls.append(
                {
                    "tool_name": tool_name,
                    "input": str(inp),
                    "output": str(output)[:500],
                    "duration_ms": duration_ms,
                }
            )

    def run(
        self,
        question: str,
        user: dict = None,
        kb_id: str = None,
        tool_calls: list | None = None,
        failure_reporter=None,
    ) -> str:
        """Run the agent on a question with answer quality reflection.

        权限上下文按 invocation 捕获在线程局部（``self._run_ctx``），不写入共享实例，
        彻底消除并发请求跨请求串写（A 的 user 被 B 清空）导致的过滤失效。
        ``user``/``kb_id`` 未显式传入时回退到构造时的默认值（向后兼容）。
        ``failure_reporter`` 是可选回调：agent 子路径失败并降级时以异常对象调用它，让进化回路
        看见这次失败（pipeline 用与流式失败相同的路径提交观测）。回调自身抛错绝不影响降级答复。
        """
        import os

        effective_user = user if user is not None else self.user
        effective_kb_id = kb_id if kb_id is not None else self.kb_id
        self._run_ctx.user = effective_user
        self._run_ctx.kb_id = effective_kb_id
        self._run_ctx.tool_calls = tool_calls
        try:
            answer, chart_paths = self._run_reflection(question)
        except Exception as error:
            # 与路由器同一类缺陷：模型/工具/递归上限的异常此前会直接终止整条请求。
            # 这里降级成可解释的答复（请求完成、失败可见），异常类进日志，异常消息与用户
            # 文本都不进答复文本。
            logger.warning("agent_run_failed exception=%s", type(error).__name__)
            if failure_reporter is not None:
                # 上报本身绝不能影响已经决定好的降级答复。
                try:
                    failure_reporter(error)
                except Exception:
                    logger.exception("agent_failure_report_failed")
            answer = f"Agent 模式执行失败（{type(error).__name__}），请改用知识库问答重试。"
            chart_paths = []
        finally:
            self._run_ctx.user = None
            self._run_ctx.kb_id = None
            self._run_ctx.tool_calls = None

        # 如果生成了图表但回答中没有路径，自动注入
        if chart_paths:
            existing = [p for p in chart_paths if os.path.exists(p)]
            if existing:
                latest = existing[-1]
                if latest not in answer:
                    answer = f"{answer}\n\n图表已保存为 {latest}"

        return answer

    def _run_reflection(self, question: str) -> tuple[str, list[str]]:
        """agent 主循环 + 答案质量自检。返回 (answer, chart_paths)。"""
        from langchain_core.messages import HumanMessage

        self._ensure_agent()

        max_reflection = 2
        current_question = question
        chart_paths = []

        for round_num in range(max_reflection + 1):
            result = self.agent.invoke(
                {"messages": [HumanMessage(content=current_question)]},
                config={"recursion_limit": self.max_iterations * 2},
            )
            messages = result.get("messages", [])
            answer = messages[-1].content if messages else "未能生成回答"
            if not isinstance(answer, str):
                answer = str(answer) if answer else "未能生成回答"

            # 从工具输出中收集图表路径
            chart_paths.extend(_extract_chart_paths(messages, self.output_dir))

            if round_num < max_reflection:
                check = _check_answer_quality(question, answer)
                if check.get("verdict") == "pass":
                    break
                missing = check.get("missing", [])
                if missing:
                    current_question = f"你之前的回答不完整，缺少以下要点：{missing}。请重新回答原始问题：{question}"
                else:
                    break
            else:
                break

        return answer, chart_paths


def _is_empty_or_error(result: str) -> bool:
    """判断工具结果是否为空或错误。"""
    if not result or not result.strip():
        return True
    # Only check Chinese error keywords to avoid false triggers on normal English text
    error_indicators = ["错误", "失败", "异常", "出错"]
    return any(ind in result for ind in error_indicators)


def _wrap_tool_with_reflection(tool, recorder=None):
    """包装工具，失败时自动重试一次。"""
    original_func = tool.func
    tool_name = tool.name

    def reflective_func(inp: str) -> str:
        started = _time.perf_counter()

        def finish(output):
            if recorder is not None:
                recorder(tool_name, inp, output, (_time.perf_counter() - started) * 1000)
            return output

        try:
            result = original_func(inp)
        except Exception as e:
            try:
                return finish(original_func(inp))
            except Exception:
                return finish(f"工具 {tool_name} 执行失败: {e}")
        if _is_empty_or_error(result):
            try:
                retry_result = original_func(inp)
                if not _is_empty_or_error(retry_result):
                    return finish(retry_result)
            except Exception:
                pass
        return finish(result)

    tool.func = reflective_func
    return tool


def _check_answer_quality(question: str, answer: str) -> dict:
    """用 LLM 自检答案质量。"""
    try:
        prompt = _pm.render("quality_check", question=question, answer=answer)
    except Exception as error:
        # prompt 取用/渲染失败（如缺少对应 prompt 版本、模板非法）属于增强项故障；
        # 此前该异常在 try 之外，会冒泡成未捕获异常并中断请求。这里降级为"通过"，
        # 让主循环采用已有答案；异常类进日志，不进答复文本。
        logger.warning("agent_quality_check_render_failed exception=%s", type(error).__name__)
        return {"verdict": "pass", "missing": [], "check_failed": type(error).__name__}
    messages = [
        {"role": "system", "content": "你是一个答案质量审查员。"},
        {"role": "user", "content": prompt},
    ]
    try:
        result = generate(messages)
    except Exception as error:
        # 质量自检是增强项：它跑不了不该终止请求。返回"通过"让主循环采用已有答案，
        # 异常类写进日志（信息不丢，也不进答复文本）。
        logger.warning("agent_quality_check_failed exception=%s", type(error).__name__)
        return {"verdict": "pass", "missing": [], "check_failed": type(error).__name__}
    try:
        return _json.loads(result)
    except _json.JSONDecodeError:
        # 尝试剥离 markdown 代码围栏
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()
        try:
            return _json.loads(cleaned)
        except _json.JSONDecodeError:
            return {"verdict": "fail", "missing": ["无法解析自检结果"]}


def route_question(question: str) -> str:
    """Classify whether a question should use RAG or Agent. Returns 'rag' or 'agent'."""
    try:
        prompt = _pm.render("router", question=question)
        messages = [
            {"role": "system", "content": "你是一个问题分类器。"},
            {"role": "user", "content": prompt},
        ]
        result = generate(messages)
    except Exception:
        # 路由阶段的模板渲染/模型调用异常一律 fail-closed 走 rag——与下面那条"其余（含空/异常）
        # 默认走 rag"的注释保持一致：没有这段捕获时，异常会直接终止整条请求。
        # （候选 #10 的补丁语义；导出补丁针对旧 base 已无法直接 apply，此处按同一语义重放。）
        return "rag"
    normalized = (result or "").strip().lower()
    # 精确白名单判定：仅 "agent" 走 agent，"rag" 走 rag，
    # 其余（含空/异常/误命中词如 agency）默认 fail-closed 走 rag。
    if normalized == "agent":
        return "agent"
    return "rag"
