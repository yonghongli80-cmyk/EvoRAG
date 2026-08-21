import time
from unittest.mock import MagicMock, patch

from rag.resilience import CircuitBreaker, retry


def test_retry_succeeds_on_first_try():
    call_count = 0

    @retry(max_attempts=3, backoff_base=0.01)
    def succeed():
        nonlocal call_count
        call_count += 1
        return "ok"

    result = succeed()
    assert result == "ok"
    assert call_count == 1


def test_retry_succeeds_after_failures():
    call_count = 0

    @retry(max_attempts=3, backoff_base=0.01, retryable_exceptions=(ValueError,))
    def fail_then_succeed():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ValueError("transient")
        return "ok"

    result = fail_then_succeed()
    assert result == "ok"
    assert call_count == 3


def test_retry_raises_after_max_attempts():
    @retry(max_attempts=3, backoff_base=0.01, retryable_exceptions=(ValueError,))
    def always_fail():
        raise ValueError("permanent")

    import pytest
    with pytest.raises(ValueError, match="permanent"):
        always_fail()


def test_retry_skips_non_retryable():
    call_count = 0

    @retry(max_attempts=3, backoff_base=0.01, retryable_exceptions=(ValueError,))
    def non_retryable():
        nonlocal call_count
        call_count += 1
        raise TypeError("not retryable")

    import pytest
    with pytest.raises(TypeError):
        non_retryable()
    assert call_count == 1


def test_circuit_breaker_starts_closed():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.1)
    assert cb.state == "closed"


def test_circuit_breaker_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1.0)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == "open"


def test_circuit_breaker_blocks_when_open():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.allow_request() is False


def test_circuit_breaker_half_open_after_timeout():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.01)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"
    time.sleep(0.02)
    assert cb.allow_request() is True
    assert cb.state == "half_open"


def test_circuit_breaker_closes_on_success():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.01)
    cb.record_failure()
    cb.record_failure()
    time.sleep(0.02)
    cb.allow_request()  # half_open
    cb.record_success()
    assert cb.state == "closed"


def test_generator_retries_on_timeout():
    """generator should retry on timeout errors."""
    from rag import generator
    original_client = generator.client
    generator.client = None
    try:
        mock_client = MagicMock()
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise TimeoutError("timeout")
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock(message=MagicMock(content="answer"))]
            return mock_resp

        mock_client.chat.completions.create.side_effect = side_effect

        with patch("rag.generator.OpenAI", return_value=mock_client):
            result = generator.generate([{"role": "user", "content": "test"}])
            assert result == "answer"
            assert call_count == 2
    finally:
        generator.client = original_client


def test_bloom_filter_blocks_unknown_keys():
    """未 set 过的 key 被布隆过滤器拦截。"""
    from rag.resilience import BloomFilter
    bf = BloomFilter(capacity=100)
    assert ("unknown" in bf) is False


def test_bloom_filter_allows_known_keys():
    """set 过的 key 可以通过布隆过滤器。"""
    from rag.resilience import BloomFilter
    bf = BloomFilter(capacity=100)
    bf.add("known")
    assert ("known" in bf) is True
