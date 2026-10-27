"""Organization KB deletion audit and terminal SQL share one commit point."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def _install_delete_failure(db, kb_id: str, failure: str) -> str:
    trigger_name = f"fail_delete_{failure}"
    escaped_kb_id = kb_id.replace("'", "''")
    with db.locked_transaction() as conn:
        if failure == "audit_insert":
            conn.execute(
                f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON org_audit_events "
                "WHEN NEW.action = 'kb_deleted' "
                f"AND NEW.target_id = '{escaped_kb_id}' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic audit insert failure'); END"
            )
        elif failure == "cleanup_sql":
            conn.execute(
                f"CREATE TRIGGER {trigger_name} BEFORE DELETE ON kb_metadata "
                f"WHEN OLD.kb_id = '{escaped_kb_id}' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic cleanup failure'); END"
            )
        elif failure == "deferred_commit":
            conn.execute("CREATE TABLE synthetic_delete_parent(id INTEGER PRIMARY KEY)")
            conn.execute(
                "CREATE TABLE synthetic_delete_child(parent_id INTEGER "
                "REFERENCES synthetic_delete_parent(id) DEFERRABLE INITIALLY DEFERRED)"
            )
            conn.execute(
                f"CREATE TRIGGER {trigger_name} AFTER DELETE ON kb_metadata "
                f"WHEN OLD.kb_id = '{escaped_kb_id}' "
                "BEGIN INSERT INTO synthetic_delete_child(parent_id) VALUES(404); END"
            )
        else:
            raise AssertionError(f"unknown synthetic failure: {failure}")
    return trigger_name


def _audit_count(db, kb_id: str) -> int:
    with db.locked_read() as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM org_audit_events "
                "WHERE action = 'kb_deleted' AND target_type = 'knowledge_base' "
                "AND target_id = ? AND result = 'success'",
                (kb_id,),
            ).fetchone()[0]
        )


@pytest.mark.parametrize(
    "failure",
    ["audit_insert", "cleanup_sql", "deferred_commit"],
    ids=["audit-insert-fails", "terminal-cleanup-fails", "deferred-fk-commit-fails"],
)
def test_org_kb_delete_audit_rolls_back_with_terminal_sql_and_retry_audits_once(
    isolated_api, make_user, monkeypatch, failure
):
    import rag.routers.knowledge as knowledge

    owner, headers = make_user("delete-audit-owner", is_admin=True)
    db = isolated_api.app.state.user_db
    kb_id = f"kb_delete_audit_{failure}"
    db.create_kb_metadata(kb_id, "Synthetic organization KB", owner["id"], "private")
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code, name, kind) VALUES('SYNTHETIC', 'Synthetic', 'team')")
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id, org_code, kb_type) VALUES(?, 'SYNTHETIC', 'organization')",
            (kb_id,),
        )

    versions_root = Path(isolated_api.app.state.document_versions_root)
    stage_dir = versions_root / "staging" / "kb" / hashlib.sha256(kb_id.encode("utf-8")).hexdigest()
    stage_dir.mkdir(parents=True)
    synthetic_file = stage_dir / "externally-cleaned-before-sql.txt"
    synthetic_file.write_text("synthetic cleanup artifact", encoding="utf-8")

    class VectorManager:
        def remove_document(self, *_args):
            return None

        def delete_kb(self, _kb_id):
            return None

    class SyntheticBM25:
        def invalidate(self, _kb_id):
            return None

        def close(self):
            return None

    class SyntheticWikiIndexer:
        def remove_kb_content(self, _kb_id, *, db):
            return None

    monkeypatch.setattr(knowledge, "KnowledgeBaseManager", VectorManager)
    monkeypatch.setattr("rag.bm25_store.BM25Store", SyntheticBM25)
    monkeypatch.setattr("rag.wiki.api.get_wiki_indexer", lambda _db: SyntheticWikiIndexer())

    trigger_name = _install_delete_failure(db, kb_id, failure)

    failed = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)

    assert failed.status_code == 503
    lifecycle = db.get_kb_lifecycle(kb_id)
    assert lifecycle["state"] == "deleting"
    assert lifecycle["last_error_code"]
    assert db.get_kb_metadata(kb_id) is not None
    with db.locked_read() as conn:
        assert conn.execute("SELECT 1 FROM kb_org_policies WHERE kb_id = ?", (kb_id,)).fetchone()
    assert _audit_count(db, kb_id) == 0
    # Filesystem cleanup is external to the SQL transaction and remains completed.
    assert not synthetic_file.exists()

    with db.locked_transaction() as conn:
        conn.execute(f"DROP TRIGGER {trigger_name}")

    retried = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)

    assert retried.status_code == 200, retried.text
    assert db.get_kb_lifecycle(kb_id)["state"] == "deleted"
    assert db.get_kb_metadata(kb_id) is None
    with db.locked_read() as conn:
        assert conn.execute("SELECT 1 FROM kb_org_policies WHERE kb_id = ?", (kb_id,)).fetchone() is None
    assert _audit_count(db, kb_id) == 1
    assert not synthetic_file.exists()
