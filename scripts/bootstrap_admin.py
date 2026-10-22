"""Create the first administrator for an empty RAGv3 deployment.

The empty deployment has no usable account: self-registration is disabled,
``INIT_ADMIN_USERNAME`` only promotes an *existing* user, and the demo seed
creates known ``123456`` accounts that ``start.py`` refuses on a remote bind.
This one-shot CLI creates exactly one strong-password administrator and marks
the matching ``org_people`` row active.  It never adds an HTTP endpoint.

Usage (Docker, before the first ``docker compose up``)::

    docker compose build rag-app
    docker compose run --rm rag-app python scripts/bootstrap_admin.py \
        --username admin

The password is prompted without echo; ``--password`` exists for automation and
is visible in the process list, so rotate it immediately after first login.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from rag.repositories.user_repo import validate_password_strength
from rag.user_db import UserDB


class BootstrapError(ValueError):
    """A user-facing bootstrap failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BootstrapError("invalid_cli_arguments", message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="bootstrap_admin.py")
    parser.add_argument("--username", required=True, help="administrator username")
    parser.add_argument(
        "--password",
        default=None,
        help="administrator password; omitted = interactive no-echo prompt",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="users.db path (default: configured RAG users database)",
    )
    parser.add_argument(
        "--display-name",
        default=None,
        help="org_people display name (default: the username)",
    )
    parser.add_argument(
        "--job-title",
        default="Administrator",
        help="org_people job title (default: Administrator)",
    )
    return parser


def _normalize_username(username: str) -> str:
    normalized = username.strip()
    if not normalized:
        raise BootstrapError("invalid_username", "用户名不能为空")
    if len(normalized) > 128:
        raise BootstrapError("invalid_username", "用户名不能超过 128 个字符")
    if any(character.isspace() for character in normalized):
        raise BootstrapError("invalid_username", "用户名不能包含空白字符")
    return normalized


def _validate_credentials(username: str, password: str) -> str:
    normalized = _normalize_username(username)
    try:
        validate_password_strength(password)
    except ValueError as error:
        raise BootstrapError("weak_password", str(error)) from error
    return normalized


def _ensure_active_org_person(db: UserDB, user_id: int, display_name: str, job_title: str) -> None:
    """Give the administrator an active org profile without inventing a unit."""
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title,active) "
            "VALUES(?,?,?,?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "display_name=excluded.display_name, job_title=excluded.job_title, active=1",
            (f"P-ADMIN-{user_id}", user_id, display_name, job_title),
        )


def bootstrap_admin(
    db: UserDB,
    username: str,
    password: str,
    *,
    display_name: str | None = None,
    job_title: str = "Administrator",
) -> int:
    """Create and promote one administrator.  Returns the new user id.

    Existing usernames are refused instead of promoted so an operator cannot
    silently take over an account created by the demo seed.
    """
    normalized = _validate_credentials(username, password)
    if db.get_user_by_username(normalized) is not None:
        raise BootstrapError("user_exists", f"用户 {normalized} 已存在；本脚本只用于空库首启")

    try:
        user_id = db.create_user(normalized, password)
    except ValueError as error:
        # Narrow the race between get_user_by_username() and create_user().
        raise BootstrapError("user_exists", str(error)) from error

    db.set_user_admin(user_id, True)
    _ensure_active_org_person(
        db,
        user_id,
        display_name.strip() if display_name and display_name.strip() else normalized,
        job_title.strip() if job_title and job_title.strip() else "Administrator",
    )
    return user_id


def _write_json(payload: object, stream) -> None:
    stream.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        password = arguments.password
        if password is None:
            if not sys.stdin.isatty():
                raise BootstrapError(
                    "password_required",
                    "非交互环境必须显式传入 --password（或改用真实终端）",
                )
            password = getpass.getpass("管理员密码: ")
        # Validate before opening the database so rejected input creates no DB.
        username = _validate_credentials(arguments.username, password)

        db_path = Path(arguments.db_path or settings.users_db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = UserDB(str(db_path))
        try:
            user_id = bootstrap_admin(
                db,
                username,
                password,
                display_name=arguments.display_name,
                job_title=arguments.job_title,
            )
            user = db.get_user_by_id(user_id)
            _write_json(
                {
                    "status": "ok",
                    "user_id": user_id,
                    "username": username,
                    "is_admin": bool(user and user["is_admin"]),
                    "org_person_active": True,
                },
                sys.stdout,
            )
        finally:
            db.close()
        return 0
    except BootstrapError as error:
        _write_json(
            {"code": error.code, "status": "error", "message": error.message},
            sys.stderr,
        )
        return 1
    except Exception:
        _write_json(
            {"code": "internal_error", "status": "error"},
            sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
