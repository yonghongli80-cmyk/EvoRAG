"""Agent module -- LangChain Agent + router."""

import json as _json
import re as _re

from rag.generator import generate
from rag.prompt_manager import PromptManager

_pm = PromptManager()


def create_agent_tools(retriever, db_path: str, output_dir: str = "data/charts", permission_filter=None) -> list:
    """Create the list of tools available to the Agent.

    ``permission_filter`` is an optional callable ``callable(list[Chunk]) -> list[Chunk]``
    applied to the retrieval results before formatting. It is used by ``RAGAgent``
    to drop chunks the current user is not allowed to see.
    """
    from langchain_core.tools import Tool

    from rag.tools import calculate, create_sql_tool

    sql_tool = create_sql_tool(db_path)

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
    sql_query_tool = Tool(
        name="sql_query",
        description="对导入的数据执行 SQL 查询。输入为 SELECT 语句。",
        func=sql_tool,
    )
    chart_tool = Tool(
        name="plot_chart",
        description="生成图表。输入格式为 '图表类型|标题|标签逗号分隔|数值逗号分隔'，如 'bar|销量对比|A,B,C|100,200,150'。",
        func=lambda inp: _parse_and_plot(inp, output_dir),
    )
    return [retrieve_tool, calc_tool, sql_query_tool, chart_tool]


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

    def __init__(self, retriever, db_path: str = ":memory:", max_iterations: int = 5, output_dir: str = "data/charts", user: dict = None, kb_id: str = "rag_docs", user_db=None):
        from langchain.agents import create_agent
        from langchain_openai import ChatOpenAI

        from config import settings

        self.max_iterations = max_iterations
        self.output_dir = output_dir
        self.user = user
        self.kb_id = kb_id or "rag_docs"
        self.user_db = user_db
        self.tools = [
            _wrap_tool_with_reflection(t)
            for t in create_agent_tools(retriever, db_path, output_dir, permission_filter=self._make_permission_filter())
        ]

        self.llm = ChatOpenAI(
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            temperature=0.3,
            model_kwargs={"extra_body": {"thinking": {"type": "disabled"}}},
        )

        system_prompt = _pm.get("agent_system")
        self.agent = create_agent(
            model=self.llm,
            tools=self.tools,
            system_prompt=system_prompt,
        )

    def _make_permission_filter(self):
        """构造 retrieve 工具结果的权限过滤回调。

        闭包读取当前 ``self.user``/``self.kb_id``/``self.user_db``：
        - user 或 user_db 缺省时不过滤（向后兼容）
        - 否则复用 pipeline 的同一套 ``filter_chunks_by_permission`` 逻辑
        """

        def filter_fn(chunks: list) -> list:
            if self.user is None or self.user_db is None:
                return chunks
            from rag.permissions import filter_chunks_by_permission

            return filter_chunks_by_permission(self.user_db, self.kb_id, chunks, self.user)

        return filter_fn

    def run(self, question: str, user: dict = None, kb_id: str = None) -> str:
        """Run the agent on a question with answer quality reflection.

        ``user``/``kb_id`` 每次调用时写入实例，供 retrieve 工具在调用时刻做权限过滤。
        """
        import os

        self.user = user
        if kb_id is not None:
            self.kb_id = kb_id

        from langchain_core.messages import HumanMessage

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

        # 如果生成了图表但回答中没有路径，自动注入
        if chart_paths:
            existing = [p for p in chart_paths if os.path.exists(p)]
            if existing:
                latest = existing[-1]
                if latest not in answer:
                    answer = f"{answer}\n\n图表已保存为 {latest}"

        return answer


def _is_empty_or_error(result: str) -> bool:
    """判断工具结果是否为空或错误。"""
    if not result or not result.strip():
        return True
    # Only check Chinese error keywords to avoid false triggers on normal English text
    error_indicators = ["错误", "失败", "异常", "出错"]
    return any(ind in result for ind in error_indicators)


def _wrap_tool_with_reflection(tool) -> "Tool":
    """包装工具，失败时自动重试一次。"""
    original_func = tool.func
    tool_name = tool.name

    def reflective_func(inp: str) -> str:
        try:
            result = original_func(inp)
        except Exception as e:
            try:
                return original_func(inp)
            except Exception:
                return f"工具 {tool_name} 执行失败: {e}"
        if _is_empty_or_error(result):
            try:
                retry_result = original_func(inp)
                if not _is_empty_or_error(retry_result):
                    return retry_result
            except Exception:
                pass
        return result

    tool.func = reflective_func
    return tool


def _check_answer_quality(question: str, answer: str) -> dict:
    """用 LLM 自检答案质量。"""
    prompt = _pm.render("quality_check", question=question, answer=answer)
    messages = [
        {"role": "system", "content": "你是一个答案质量审查员。"},
        {"role": "user", "content": prompt},
    ]
    result = generate(messages)
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
    prompt = _pm.render("router", question=question)
    messages = [
        {"role": "system", "content": "你是一个问题分类器。"},
        {"role": "user", "content": prompt},
    ]
    result = generate(messages)
    normalized = (result or "").strip().lower()
    # 精确白名单判定：仅 "agent" 走 agent，"rag" 走 rag，
    # 其余（含空/异常/误命中词如 agency）默认 fail-closed 走 rag。
    if normalized == "agent":
        return "agent"
    return "rag"
