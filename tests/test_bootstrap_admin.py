"""F-03: one-shot first-admin bootstrap for an empty deployment."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_admin_under_test", ROOT / "scripts" / "bootstrap_admin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap_admin = _load_script()
STRONG_PASSWORD = "Str0ng-Pass1!"


def test_bootstrap_creates_admin_that_can_log_in(isolated_api):
    db = isolated_api.app.state.user_db

    user_id = bootstrap_admin.bootstrap_admin(db, "first-admin", STRONG_PASSWORD)

    user = db.get_user_by_id(user_id)
    assert user is not None
    assert user["is_admin"] is True
    with db.locked_read() as conn:
        person = conn.execute(
            "SELECT active FROM org_people WHERE user_id=?", (user_id,)
        ).fetchone()
    assert person is not None and person["active"] == 1

    response = isolated_api.post(
        "/login",
        json={"username": "first-admin", "password": STRONG_PASSWORD},
    )
    assert response.status_code == 200
    me = isolated_api.get("/me")
    assert me.status_code == 200
    assert me.json()["is_admin"] is True


@pytest.mark.parametrize(
    "weak_password",
    (
        "123456",  # the seed default from rag/org_auth/seed.py
        "short1A",  # too short
        "alllower1",  # no uppercase
        "ALLUPPER1",  # no lowercase
        "NoDigitsHere",  # no digit
    ),
)
def test_bootstrap_rejects_weak_passwords_without_creating_the_user(
    isolated_api, weak_password
):
    db = isolated_api.app.state.user_db

    with pytest.raises(bootstrap_admin.BootstrapError) as error:
        bootstrap_admin.bootstrap_admin(db, "weak-admin", weak_password)

    assert error.value.code == "weak_password"
    assert db.get_user_by_username("weak-admin") is None


def test_bootstrap_refuses_an_existing_user_and_does_not_promote_it(isolated_api):
    db = isolated_api.app.state.user_db
    user_id = db.create_user("already-here", STRONG_PASSWORD)

    with pytest.raises(bootstrap_admin.BootstrapError) as error:
        bootstrap_admin.bootstrap_admin(db, "already-here", "Another-Pass1!")

    assert error.value.code == "user_exists"
    assert db.get_user_by_id(user_id)["is_admin"] is False


def test_cli_creates_admin_in_an_explicit_temporary_database(tmp_path, capsys):
    db_path = tmp_path / "users.db"

    exit_code = bootstrap_admin.main(
        [
            "--db-path",
            str(db_path),
            "--username",
            "cli-admin",
            "--password",
            STRONG_PASSWORD,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["username"] == "cli-admin"
    assert payload["is_admin"] is True
    assert payload["org_person_active"] is True

    from rag.user_db import UserDB

    db = UserDB(str(db_path))
    try:
        user = db.get_user_by_username("cli-admin")
        assert user is not None and user["is_admin"] is True
        assert db.authenticate("cli-admin", STRONG_PASSWORD) is not None
    finally:
        db.close()


def test_bootstrap_admin_does_not_trip_the_remote_initial_password_guard(tmp_path):
    """A bootstrapped strong password must allow the Docker 0.0.0.0 bind."""
    from types import SimpleNamespace

    import start
    from rag.user_db import UserDB

    db = UserDB(str(tmp_path / "users.db"))
    try:
        bootstrap_admin.bootstrap_admin(db, "guard-admin", STRONG_PASSWORD)
        config = SimpleNamespace(
            auth_enabled=True,
            allow_initial_passwords_remote=False,
        )
        assert start._remote_deployment_error("0.0.0.0", config, db) is None
    finally:
        db.close()


def test_deployment_docs_explain_the_empty_database_and_seed_conflict():
    docs = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    assert "docker compose run --rm rag-app python scripts/bootstrap_admin.py" in docs
    assert "INIT_ADMIN_USERNAME" in docs
    assert "RAG_ALLOW_INITIAL_PASSWORDS_REMOTE=true" in docs
    assert "123456" in docs
    assert "demo001" in docs


def test_cli_refuses_weak_password_with_json_error(tmp_path, capsys):
    exit_code = bootstrap_admin.main(
        [
            "--db-path",
            str(tmp_path / "users.db"),
            "--username",
            "cli-admin",
            "--password",
            "123456",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.err)["code"] == "weak_password"
    assert not (tmp_path / "users.db").exists()
