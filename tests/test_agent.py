"""Tests for agent module."""
from unittest.mock import MagicMock, patch


def test_agent_has_four_tools():
    """Agent should register 4 tools."""
    from rag.agent import create_agent_tools
    tools = create_agent_tools(retriever=MagicMock(), db_path=":memory:")
    tool_names = [t.name for t in tools]
    assert "retrieve" in tool_names
    assert "calculate" in tool_names
    assert "sql_query" in tool_names
    assert "plot_chart" in tool_names


@patch("langchain_openai.ChatOpenAI")
def test_agent_defers_llm_creation_until_run(mock_chat_cls):
    """Constructing an Agent must not require a configured model credential."""
    from rag.agent import RAGAgent

    agent = RAGAgent(retriever=MagicMock(), db_path=":memory:", max_iterations=2)

    assert agent.max_iterations == 2
    mock_chat_cls.assert_not_called()


@patch("rag.agent.generate")
def test_router_simple_question(mock_generate):
    """Simple question should return 'rag'."""
    mock_generate.return_value = "rag"
    from rag.agent import route_question
    result = route_question("What is RAG?")
    assert result == "rag"


@patch("rag.agent.generate")
def test_router_complex_question(mock_generate):
    """Complex question should return 'agent'."""
    mock_generate.return_value = "agent"
    from rag.agent import route_question
    result = route_question("Calculate YoY growth for products A and B and generate a chart")
    assert result == "agent"


@patch("langchain_openai.ChatOpenAI")
def test_agent_disables_thinking_mode(mock_chat_cls):
    """Agent LLM should disable DeepSeek thinking mode."""
    from rag.agent import RAGAgent

    agent = RAGAgent(retriever=MagicMock(), db_path=":memory:")
    agent._ensure_agent()

    call_kwargs = mock_chat_cls.call_args.kwargs
    assert call_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "model_kwargs" not in call_kwargs


def test_tool_wrapper_retries_on_exception():
    """包装后的工具在异常时自动重试。"""
    from langchain_core.tools import Tool

    from rag.agent import _wrap_tool_with_reflection

    call_count = 0

    def failing_func(inp):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("transient error")
        return "success"

    tool = Tool(name="test_tool", description="test", func=failing_func)
    wrapped = _wrap_tool_with_reflection(tool)

    result = wrapped.func("input")
    assert result == "success"
    assert call_count == 2


def test_tool_wrapper_returns_error_after_retry_fails():
    """重试仍失败时返回错误信息而非抛异常。"""
    from langchain_core.tools import Tool

    from rag.agent import _wrap_tool_with_reflection

    def always_fail(inp):
        raise ValueError("permanent error")

    tool = Tool(name="test_tool", description="test", func=always_fail)
    wrapped = _wrap_tool_with_reflection(tool)

    result = wrapped.func("input")
    assert "error" in result.lower() or "失败" in result


def test_tool_wrapper_passes_through_on_success():
    """正常调用不触发重试。"""
    from langchain_core.tools import Tool

    from rag.agent import _wrap_tool_with_reflection

    tool = Tool(name="test_tool", description="test", func=lambda x: f"result: {x}")
    wrapped = _wrap_tool_with_reflection(tool)

    result = wrapped.func("hello")
    assert result == "result: hello"


def test_check_answer_quality_pass():
    """自检通过时不重跑。"""
    from rag.agent import _check_answer_quality

    with patch("rag.agent.generate", return_value='{"verdict": "pass", "missing": []}'):
        result = _check_answer_quality("什么是 Raft？", "Raft 是一种一致性协议")
        assert result["verdict"] == "pass"


def test_check_answer_quality_fail():
    """自检失败时返回缺失要点。"""
    from rag.agent import _check_answer_quality

    with patch("rag.agent.generate", return_value='{"verdict": "fail", "missing": ["选举机制"]}'):
        result = _check_answer_quality("什么是 Raft？", "Raft 是一种协议")
        assert result["verdict"] == "fail"
        assert "选举机制" in result["missing"]


@patch("rag.agent.generate")
def test_router_fails_closed_when_the_model_call_raises(mock_generate):
    """路由阶段的异常必须 fail-closed 走 rag——否则一条抖动会终止整条请求。

    这段是候选 #10 的补丁语义：原来的注释已经写着"其余（含空/异常）默认走 rag"，
    但代码没有捕获异常，注释与实现不一致。
    """
    mock_generate.side_effect = ValueError("router exploded")

    from rag.agent import route_question

    assert route_question("What is RAG?") == "rag"


@patch("rag.agent.generate")
def test_router_fails_closed_when_the_prompt_render_raises(mock_generate):
    """模板渲染失败同样 fail-closed：异常来自哪一步都要保住请求。"""
    from rag.agent import route_question

    with patch("rag.agent._pm.render", side_effect=KeyError("router")):
        assert route_question("What is RAG?") == "rag"
    mock_generate.assert_not_called()


def test_the_agent_run_degrades_instead_of_killing_the_request():
    """Agent 运行期的异常必须降级成可解释答复，而不是终止整条请求。

    与路由器同一类缺陷（见候选 #10）：模型/工具/LangChain 递归上限的异常此前会一路冒到
    API 层，整条请求以 error 收场。降级答复里带异常**类名**（可观测），不带异常消息与用户文本。
    """
    from rag.agent import RAGAgent

    agent = RAGAgent(retriever=MagicMock(), db_path=":memory:")

    with patch.object(RAGAgent, "_run_reflection", side_effect=RuntimeError("model exploded")):
        answer = agent.run("帮我算一下同比")

    assert "Agent 模式执行失败" in answer
    assert "RuntimeError" in answer
    assert "model exploded" not in answer  # 异常消息不进答复
    assert agent._run_ctx.user is None  # 线程局部上下文仍被清理


def test_the_quality_check_failure_does_not_kill_the_answer():
    """质量自检是增强项：它的模型调用失败不得终止请求，也不得让主循环空转重试。"""
    from rag.agent import _check_answer_quality

    with patch("rag.agent.generate", side_effect=TimeoutError("slow")):
        result = _check_answer_quality("问题", "答案")

    assert result["verdict"] == "pass"
    assert result["missing"] == []
    assert result["check_failed"] == "TimeoutError"


def test_the_agent_reports_the_failure_that_caused_the_degradation():
    """降级时必须把异常交给上报回调：否则 Agent 失败对进化回路完全不可见。"""
    from rag.agent import RAGAgent

    agent = RAGAgent(retriever=MagicMock(), db_path=":memory:")
    reported = []

    with patch.object(RAGAgent, "_run_reflection", side_effect=RuntimeError("model exploded")):
        answer = agent.run("帮我算一下同比", failure_reporter=reported.append)

    assert "Agent 模式执行失败" in answer
    assert [type(error).__name__ for error in reported] == ["RuntimeError"]


def test_a_broken_failure_reporter_never_breaks_the_answer():
    """上报回调自身抛错绝不能影响已经决定好的降级答复。"""
    from rag.agent import RAGAgent

    agent = RAGAgent(retriever=MagicMock(), db_path=":memory:")

    def broken(_error):
        raise OSError("reporter exploded")

    with patch.object(RAGAgent, "_run_reflection", side_effect=RuntimeError("boom")):
        answer = agent.run("帮我算一下同比", failure_reporter=broken)

    assert "Agent 模式执行失败（RuntimeError）" in answer
