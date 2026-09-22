from unittest.mock import MagicMock, patch

from rag.generator import generate


@patch("rag.generator.client")
def test_generate_returns_answer(mock_client):
    mock_message = MagicMock()
    mock_message.content = "RAG stands for Retrieval Augmented Generation."
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_response

    result = generate([
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is RAG?"},
    ])

    assert result == "RAG stands for Retrieval Augmented Generation."
    mock_client.chat.completions.create.assert_called_once()


@patch("rag.generator.client")
def test_generate_passes_messages_through(mock_client):
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = "answer"
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_response

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    result = generate(msgs)

    assert result == "answer"
    call_args = mock_client.chat.completions.create.call_args
    assert call_args.kwargs["messages"] == msgs


def test_generate_accepts_temperature_parameter():
    """generate() 应该接受 temperature 参数。"""
    from rag.generator import generate
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "test answer"
    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock(return_value=mock_response)

    with patch("rag.generator.client", mock_client):
        generate([{"role": "user", "content": "test"}], temperature=0.7)

    call_kwargs = mock_client.chat.completions.create.call_args[1]
    assert call_kwargs.get("temperature") == 0.7


def test_deepseek_default_headers_empty_by_default(monkeypatch):
    """Without configuration the standard Bearer authorization is the only scheme."""
    from config import settings
    from rag.generator import deepseek_default_headers

    monkeypatch.setattr(settings, "deepseek_auth_header", "")
    assert deepseek_default_headers() is None


def test_deepseek_default_headers_carry_configured_scheme(monkeypatch):
    """Some OpenAI-compatible providers authenticate with a custom header name."""
    from config import settings
    from rag.generator import deepseek_default_headers

    monkeypatch.setattr(settings, "deepseek_auth_header", "x-api-key")
    monkeypatch.setattr(settings, "deepseek_api_key", "key-value")
    assert deepseek_default_headers() == {"x-api-key": "key-value"}


def test_generate_builds_client_with_configured_headers(monkeypatch):
    """The lazily built client forwards the configured authentication header."""
    from config import settings

    monkeypatch.setattr(settings, "deepseek_auth_header", "x-api-key")
    monkeypatch.setattr(settings, "deepseek_api_key", "key-value")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://provider.example/v1")
    monkeypatch.setattr(settings, "deepseek_model", "vendor/model")

    import rag.generator as generator

    monkeypatch.setattr(generator, "client", None)
    recorded = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

    monkeypatch.setattr(generator, "OpenAI", FakeOpenAI)

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "answer"
    fake = MagicMock()
    fake.chat.completions.create.return_value = mock_response

    def fake_openai(**kwargs):
        recorded["instance"] = FakeOpenAI(**kwargs)
        return fake

    monkeypatch.setattr(generator, "OpenAI", fake_openai)
    generator.generate([{"role": "user", "content": "hi"}])

    assert recorded["default_headers"] == {"x-api-key": "key-value"}
    assert recorded["base_url"] == "https://provider.example/v1"
