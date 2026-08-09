from unittest.mock import patch

import pytest

from rag.resilience import retry


def test_default_retry_does_not_retry_programming_errors():
    calls = 0

    @retry(max_attempts=3, backoff_base=0)
    def broken():
        nonlocal calls
        calls += 1
        raise KeyError("bug")

    with patch("rag.resilience.time.sleep") as sleep:
        with pytest.raises(KeyError, match="bug"):
            broken()

    assert calls == 1
    sleep.assert_not_called()


def test_default_retry_retries_transient_errors_with_capped_backoff():
    calls = 0

    @retry(max_attempts=4, backoff_base=3, max_delay=5)
    def unavailable():
        nonlocal calls
        calls += 1
        raise TimeoutError("temporary")

    with patch("rag.resilience.time.sleep") as sleep:
        with pytest.raises(TimeoutError, match="temporary"):
            unavailable()

    assert calls == 4
    assert [call.args[0] for call in sleep.call_args_list] == [3, 5, 5]
