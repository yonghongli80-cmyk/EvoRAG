import json

import pytest

from config import Settings
from rag.backups.models import BackupError, context_from_settings
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


def test_cli_failure_returns_nonzero_without_absolute_path(backup_context, capsys, monkeypatch):
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


def test_cli_verify_restore_and_list_use_backup_ids(backup_context, tmp_path, capsys):
    created = create_backup(backup_context, "manual", now=1_777_000_000.0)

    assert main(["verify", created.backup_id], context_factory=lambda: backup_context) == 0
    assert main(["list"], context_factory=lambda: backup_context) == 0
    assert (
        main(
            ["restore-test", created.backup_id, str(tmp_path / "isolation")],
            context_factory=lambda: backup_context,
        )
        == 0
    )

    assert created.backup_id in capsys.readouterr().out


@pytest.mark.parametrize("backup_id", ["../escape", "a/b", r"a\b", ""])
def test_cli_rejects_unsafe_backup_ids(backup_context, backup_id, capsys):
    code = main(["verify", backup_id], context_factory=lambda: backup_context)

    assert code != 0
    assert "unsafe_backup_id" in capsys.readouterr().err


def test_cli_restore_protects_external_qdrant_and_allows_independent_target(tmp_path, capsys):
    data_root = tmp_path / "configured-data"
    backup_root = tmp_path / "configured-backups"
    qdrant_root = tmp_path / "container" / "external-qdrant"
    configured = Settings(
        _env_file=None,
        users_db_path=str(data_root / "users.db"),
        analysis_db_path=str(data_root / "analysis.db"),
        memory_db_path=str(data_root / "memory.db"),
        document_versions_root=str(data_root / "versions"),
        structured_datasets_root=str(data_root / "datasets"),
        backup_root=str(backup_root),
        qdrant_path=str(qdrant_root),
    )

    def context_factory():
        return context_from_settings(configured)

    context = context_factory()
    create_backup_sources(context)
    created = create_backup(context, "manual", now=1_777_000_000.0)
    sentinel = qdrant_root / "sentinel.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("keep", encoding="utf-8")

    overlapping_targets = (
        qdrant_root,
        qdrant_root.parent,
        qdrant_root / "nested-isolation",
    )
    for isolation_root in overlapping_targets:
        code = main(
            ["restore-test", created.backup_id, str(isolation_root)],
            context_factory=context_factory,
        )
        assert code == 1
        error = json.loads(capsys.readouterr().err)
        assert error == {
            "code": "restore_target_protected",
            "status": "error",
        }
        assert sentinel.read_text(encoding="utf-8") == "keep"
        assert not (isolation_root / created.backup_id).exists()

    independent_root = tmp_path / "safe-isolation"
    code = main(
        ["restore-test", created.backup_id, str(independent_root)],
        context_factory=context_factory,
    )

    assert code == 0
    assert (independent_root / created.backup_id / "restore-report.json").is_file()
    assert sentinel.read_text(encoding="utf-8") == "keep"
