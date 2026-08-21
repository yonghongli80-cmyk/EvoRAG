"""查询改写模块测试"""
from unittest.mock import patch


def test_rewrite_query_returns_formal_version():
    """口语化问题应被改写为更正式的版本"""
    from rag.query_rewriter import rewrite_query

    with patch("rag.generator.generate", return_value="服务实例健康检查失败后如何自动从注册中心摘除？"):
        result = rewrite_query("服务挂了怎么自动摘除？")

    assert "健康检查" in result or "摘除" in result
    assert result != "服务挂了怎么自动摘除？"


def test_rewrite_query_preserves_technical_terms():
    """已包含技术术语的问题应保留原意"""
    from rag.query_rewriter import rewrite_query

    with patch("rag.generator.generate", return_value="mTLS 的三种配置模式分别是什么？"):
        result = rewrite_query("mTLS 有哪三种配置模式？")

    assert "mTLS" in result


def test_rewrite_query_returns_original_on_failure():
    """API 失败时应返回原问题，不抛异常"""
    from rag.query_rewriter import rewrite_query

    with patch("rag.generator.generate", side_effect=Exception("API down")):
        result = rewrite_query("服务挂了怎么办？")

    assert result == "服务挂了怎么办？"


def test_rewrite_query_uses_generate_module():
    """应复用 generator.generate() 而非直接调 API"""
    from rag.query_rewriter import rewrite_query

    with patch("rag.generator.generate", return_value="改写结果") as mock_gen:
        rewrite_query("测试问题")

    mock_gen.assert_called_once()
    messages = mock_gen.call_args[0][0]
    assert messages[0]["role"] == "system"
    assert "改写" in messages[0]["content"]
    assert "测试问题" in messages[1]["content"]


def test_rewrite_query_returns_original_on_empty_result():
    """generate 返回空字符串时应返回原问题"""
    from rag.query_rewriter import rewrite_query

    with patch("rag.generator.generate", return_value=""):
        result = rewrite_query("测试问题")

    assert result == "测试问题"
