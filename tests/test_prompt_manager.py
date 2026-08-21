"""Tests for prompt manager."""
import pytest


@pytest.fixture
def pm(tmp_path):
    """创建带测试 prompt 的临时目录。"""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "test.yaml").write_text(
        "name: test\nversion: 2\ndescription: test prompt\n"
        "changelog:\n  - version: 2\n    date: 2026-05-29\n    change: v2\n"
        "  - version: 1\n    date: 2026-05-23\n    change: v1\n"
        "template: |\n  Hello {name}, v2\n",
        encoding="utf-8",
    )
    (prompt_dir / "test_v1.yaml").write_text(
        "name: test\nversion: 1\ndescription: test prompt v1\n"
        "changelog:\n  - version: 1\n    date: 2026-05-23\n    change: v1\n"
        "template: |\n  Hi {name}\n",
        encoding="utf-8",
    )
    return __import__("rag.prompt_manager", fromlist=["PromptManager"]).PromptManager(str(prompt_dir))


def test_get_latest_version(pm):
    """version=None 加载最新版本。"""
    template = pm.get("test")
    assert "Hello" in template


def test_get_specific_version(pm):
    """指定版本加载。"""
    template = pm.get("test", version=1)
    assert "Hi" in template


def test_render(pm):
    """模板变量渲染。"""
    result = pm.render("test", name="World")
    assert "Hello World" in result


def test_list_versions(pm):
    """列出所有版本。"""
    versions = pm.list_versions("test")
    assert len(versions) == 2
    assert versions[0]["version"] == 2


def test_not_found(pm):
    """不存在的 prompt 抛出错误。"""
    with pytest.raises(FileNotFoundError):
        pm.get("nonexistent")


def test_version_not_found(pm):
    """不存在的版本抛出错误。"""
    with pytest.raises(ValueError):
        pm.get("test", version=99)


def test_query_rewriter_uses_prompt_manager():
    """query_rewriter 应从 prompt_manager 加载 prompt。"""
    from unittest.mock import patch

    from rag.query_rewriter import rewrite_query

    with patch("rag.generator.generate", return_value="改写结果"):
        result = rewrite_query("测试问题")
        assert result == "改写结果"


def test_route_question_uses_prompt_manager():
    """route_question 应从 prompt_manager 加载 prompt。"""
    from unittest.mock import patch

    from rag.agent import route_question

    with patch("rag.agent.generate", return_value="rag"):
        result = route_question("简单问题")
        assert result == "rag"
