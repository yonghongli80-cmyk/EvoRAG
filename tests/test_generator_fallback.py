"""Tests for generator circuit-breaker degradation (identifiable failure)."""

from unittest.mock import MagicMock, patch

import pytest

from rag.generator import _breaker, generate
from rag.resilience import CircuitOpenError


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
    """When the circuit breaker is open, generate() must raise a dedicated error.

    一句"系统繁忙"不是模型输出：它被改写缓存当成改写结果、被 pipeline 当成正式答案
    写进 memory 与轨迹（2026-10-22 全库评审 P1-01/P1-02）。降级必须是可识别失败。
    """

    def setup_method(self):
        _reset_breaker()

    def teardown_method(self):
        _reset_breaker()

    def test_open_breaker_raises_a_circuit_open_error(self):
        """Circuit open -> generate() raises CircuitOpenError, never returns the busy copy."""
        _force_breaker_open()

        with pytest.raises(CircuitOpenError):
            generate([{"role": "user", "content": "hello"}])

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
            result = generate([{"role": "user", "content": "hello"}])

        assert result == "Hello from the model"
