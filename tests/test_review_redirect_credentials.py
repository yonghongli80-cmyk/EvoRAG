from unittest.mock import MagicMock, patch

import pytest
import requests

from rag.data_sources.base import safe_requests_get


def _redirect(location):
    response = MagicMock(spec=requests.Response)
    response.is_redirect = True
    response.status_code = 302
    response.headers = {"Location": location}
    return response


def _ok():
    response = MagicMock(spec=requests.Response)
    response.is_redirect = False
    response.status_code = 200
    response.headers = {}
    return response


def test_same_origin_redirect_keeps_caller_headers(monkeypatch):
    monkeypatch.setattr("rag.data_sources.base.validate_external_url", lambda url: None)
    redirect, final = _redirect("/next"), _ok()
    headers = {"Authorization": "provided-by-test", "x-api-key": "provided-by-test"}

    with patch(
        "rag.data_sources.base.requests.get", side_effect=[redirect, final]
    ) as get:
        result = safe_requests_get("https://public.example/path", headers=headers)

    assert result is final
    assert [entry.kwargs["headers"] for entry in get.call_args_list] == [headers, headers]
    redirect.close.assert_called_once()


@pytest.mark.parametrize(
    "location",
    [
        "https://other.example/private",
        "https://public.example:444/private",
        "http://public.example/private",
    ],
)
def test_cross_origin_redirect_strips_credentials_permanently(location, monkeypatch):
    monkeypatch.setattr("rag.data_sources.base.validate_external_url", lambda url: None)
    first, second, final = _redirect(location), _redirect("https://public.example/return"), _ok()
    headers = {"x-custom-secret": "provided-by-test", "Accept": "application/json"}
    kwargs = {
        "auth": ("user", "provided-by-test"),
        "cookies": {"session": "provided-by-test"},
        "params": {"token": "provided-by-test"},
    }

    with patch(
        "rag.data_sources.base.requests.get", side_effect=[first, second, final]
    ) as get:
        result = safe_requests_get(
            "https://public.example/start", headers=headers, **kwargs
        )

    assert result is final
    first_call, cross_call, return_call = get.call_args_list
    assert first_call.kwargs["headers"] == headers
    assert first_call.kwargs["auth"] == kwargs["auth"]
    assert first_call.kwargs["cookies"] == kwargs["cookies"]
    assert first_call.kwargs["params"] == kwargs["params"]
    for call_args in (cross_call, return_call):
        assert call_args.kwargs["headers"] == {}
        assert "auth" not in call_args.kwargs
        assert "cookies" not in call_args.kwargs
        assert "params" not in call_args.kwargs
    first.close.assert_called_once()
    second.close.assert_called_once()


def test_max_redirect_response_is_closed_before_error(monkeypatch):
    monkeypatch.setattr("rag.data_sources.base.validate_external_url", lambda url: None)
    response = _redirect("/next")
    with (
        patch("rag.data_sources.base.requests.get", return_value=response),
        pytest.raises(ValueError, match="重定向次数过多"),
    ):
        safe_requests_get("https://public.example/start", max_redirects=0)
    response.close.assert_called_once()
