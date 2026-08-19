import json

import pytest

from rag.backups.models import BackupError
from rag.backups.service import create_backup
from scripts.backup import main
from tests.backup_fixtures import create_backup_sources, make_backup_context


@pytest.fixture
def backup_context(tmp_path):
    context = make_backup_context(tmp_path)
    create_backup_sources(context)
    return context


def test_cli_create_if_due_prints_stable_json(backup_context, capsys):
    code = main(
        ["create-if-due", "--trigger", "deploy"],
        context_factory=lambda: backup_context,
        now=lambda: 1_777_000_000.0,
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "verified"
    assert payload["vector_rebuild_required"] is True
    assert "path" not in payload


def test_cli_failure_returns_nonzero_without_absolute_path(
    backup_context, capsys, monkeypatch
):
    def fail_create(*_args, **_kwargs):
        raise BackupError("disk_full")

    monkeypatch.setattr("scripts.backup.create_if_due", fail_create)
    code = main(
        ["create-if-due", "--trigger", "deploy"],
        context_factory=lambda: backup_context,
    )
    output = capsys.readouterr().err

    assert code == 1
    assert json.loads(output) == {"status": "error", "code": "disk_full"}
    assert str(backup_context.backup_root) not in output


def test_cli_verify_restore_and_list_use_backup_ids(
    backup_context, tmp_path, capsys
):
    created = create_backup(backup_context, "manual", now=1_777_000_000.0)

    assert main(
        ["verify", created.backup_id], context_factory=lambda: backup_context
    ) == 0
    assert main(["list"], context_factory=lambda: backup_context) == 0
    assert main(
        ["restore-test", created.backup_id, str(tmp_path / "isolation")],
        context_factory=lambda: backup_context,
    ) == 0

    assert created.backup_id in capsys.readouterr().out


@pytest.mark.parametrize("backup_id", ["../escape", "a/b", r"a\b", ""])
def test_cli_rejects_unsafe_backup_ids(backup_context, backup_id, capsys):
    code = main(
        ["verify", backup_id], context_factory=lambda: backup_context
    )

    assert code != 0
    assert "unsafe_backup_id" in capsys.readouterr().err
