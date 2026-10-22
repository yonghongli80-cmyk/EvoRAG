"""F-07: a small subset of the suite runs with authentication **enabled**.

``tests/conftest.py`` pins ``RAG_AUTH_ENABLED=false`` so the ~2500 existing tests can call
endpoints anonymously.  That default is also how authentication regressions stay invisible:
a route that forgets ``Depends(require_user)`` returns 200 to an anonymous caller and every
test still passes (F-07 in the 2026-10-22 review; the same shape as FE-01/E-04).

This module is the counterweight.  It uses the same isolated database/client as the rest of
the suite, but with ``settings.auth_enabled = True``, and only asserts the *guard* contract:
anonymous → 401, wrong credentials → 401, member → 403 on admin routes, admin → allowed.
It is deliberately small — it is a smoke net, not a second full API suite.
"""

from __future__ import annotations

import pytest

PASSWORD = "Password-123!"


def test_health_stays_public_with_auth_enabled(isolated_api):
    """The Compose healthcheck has no credentials; it must keep working."""
    response = isolated_api.get("/health")

    assert response.status_code in (200, 503)
    assert response.json()["status"] in {"healthy", "degraded"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/me"),
        ("GET", "/knowledge-bases"),
        ("POST", "/query"),
        ("GET", "/conversations"),
    ],
)
def test_anonymous_requests_are_rejected(isolated_api, method, path):
    response = isolated_api.request(method, path, json={} if method == "POST" else None)

    assert response.status_code == 401


def test_login_issues_a_token_and_a_session_cookie(isolated_api, make_user):
    make_user("smoke_alice")

    response = isolated_api.post(
        "/login", json={"username": "smoke_alice", "password": PASSWORD}
    )

    assert response.status_code == 200
    assert response.json()["token"]
    assert isolated_api.cookies.get("rag_session")

    # The cookie alone authenticates the next request (the browser path).
    me = isolated_api.get("/me")
    assert me.status_code == 200
    assert me.json()["username"] == "smoke_alice"


def test_bearer_token_authenticates_requests(isolated_api, make_user):
    _user, headers = make_user("smoke_bob")

    response = isolated_api.get("/me", headers=headers)

    assert response.status_code == 200
    assert response.json()["username"] == "smoke_bob"


def test_wrong_password_is_rejected(isolated_api, make_user):
    make_user("smoke_carol")

    response = isolated_api.post(
        "/login", json={"username": "smoke_carol", "password": "Wrong-Password-1!"}
    )

    assert response.status_code == 401


def test_invalid_bearer_token_is_rejected(isolated_api):
    response = isolated_api.get("/me", headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 401


def test_admin_only_endpoint_rejects_members(isolated_api, make_user):
    _member, member_headers = make_user("smoke_dave")
    target, _target_headers = make_user("smoke_erin")

    response = isolated_api.put(
        f"/users/{target['id']}/role", json={"is_admin": True}, headers=member_headers
    )

    assert response.status_code == 403


def test_admin_only_endpoint_allows_admins(isolated_api, make_user):
    admin, admin_headers = make_user("smoke_frank", is_admin=True)
    target, _target_headers = make_user("smoke_grace")

    response = isolated_api.put(
        f"/users/{target['id']}/role", json={"is_admin": True}, headers=admin_headers
    )

    assert response.status_code == 200
    assert response.json()["is_admin"] in (1, True)
    assert admin["is_admin"] in (1, True)


def test_logout_clears_the_session_cookie(isolated_api, make_user):
    make_user("smoke_heidi")
    isolated_api.post("/login", json={"username": "smoke_heidi", "password": PASSWORD})
    assert isolated_api.get("/me").status_code == 200

    assert isolated_api.post("/logout").status_code == 204

    assert isolated_api.get("/me").status_code == 401
