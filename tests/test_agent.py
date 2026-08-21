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
