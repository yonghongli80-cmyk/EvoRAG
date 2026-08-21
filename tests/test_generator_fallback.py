"""Tests for generator circuit-breaker fallback (graceful degradation)."""

from unittest.mock import MagicMock, patch

from rag.generator import _breaker, generate


def _force_breaker_open():
    """Force the module-level breaker into the 'open' state."""
    for _ in range(_breaker.failure_threshold):
        _breaker.record_failure()
    assert _breaker.state == "open"


def _reset_breaker():
    """Reset breaker back to closed / healthy."""
    _breaker.record_success()
    assert _breaker.state == "closed"


class TestGeneratorCircuitBreakerFallback:
    """When the circuit breaker is open, generate() must return a friendly
    fallback string instead of raising an exception."""

    def setup_method(self):
        _reset_breaker()

    def teardown_method(self):
        _reset_breaker()

    def test_open_breaker_returns_fallback_string(self):
        """Circuit open -> generate() returns a string containing '系统繁忙'."""
        _force_breaker_open()

        result = generate([
            {"role": "user", "content": "hello"},
        ])

        assert isinstance(result, str)
        assert "系统繁忙" in result

    def test_open_breaker_does_not_raise(self):
        """Circuit open -> generate() must NOT raise any exception."""
        _force_breaker_open()

        # Should not raise
        result = generate([
            {"role": "user", "content": "hello"},
        ])
        assert result is not None

    def test_breaker_recovery_returns_normal_answer(self):
        """After the breaker recovers, generate() works normally."""
        _force_breaker_open()

        # Simulate recovery: reset the breaker
        _reset_breaker()

        mock_message = MagicMock()
        mock_message.content = "Hello from the model"
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        with patch("rag.generator.client") as mock_client:
            mock_client.chat.completions.create.return_value = mock_response
            result = generate([
                {"role": "user", "content": "hello"},
            ])

        assert result == "Hello from the model"
