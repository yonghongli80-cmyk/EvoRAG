import pytest
from pydantic import ValidationError

from rag.login_throttle import LoginThrottle
from rag.routers.auth import LoginRequest


def test_login_credentials_are_bounded():
    with pytest.raises(ValidationError):
        LoginRequest(username="u" * 65, password="x")
    with pytest.raises(ValidationError):
        LoginRequest(username="u", password="p" * 257)


def test_login_throttle_evicts_old_keys_instead_of_growing_forever():
    throttle = LoginThrottle(limit=5, window_seconds=60, max_keys=2)
    for index in range(100):
        throttle.record_failure(f"host:{index}")
    assert len(throttle._attempts) <= 2
