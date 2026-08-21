"""Conservative identification and removal of historical test artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass

_TEST_USER = re.compile(r"^_(?:test(?:_|$)|perm_|dl_test$|data_source_test_)")
_TEST_KB = re.compile(r"^kb_(?:test|perm|priv_test|no_auth_test)(?:.*)$")
_TEST_KB_NAME = re.compile(r"^(?:test(?:_|$)|priv_test$|perm_test$|no_auth_test$)")


@dataclass(frozen=True)
class CleanupCandidates:
    user_ids: tuple[int, ...]
    usernames: tuple[str, ...]
    kb_ids: tuple[str, ...]


def _placeholders(values: tuple[object, ...]) -> str:
    return ",".join("?" for _ in values)


def find_candidates(db) -> CleanupCandidates:
    """Return only artifacts whose stable keys and relationships prove test origin."""
    with db.locked_read() as conn:
        kb_rows = conn.execute(
            "SELECT m.kb_id,m.name,p.kb_type,p.org_code,p.seed_key "
            "FROM kb_metadata m JOIN kb_org_policies p ON p.kb_id=m.kb_id"
        ).fetchall()
        kb_ids = tuple(sorted(
            row["kb_id"] for row in kb_rows
            if _TEST_KB.fullmatch(row["kb_id"])
            and (not row["name"] or _TEST_KB_NAME.match(row["name"]))
            and row["kb_type"] == "unassigned"
            and row["org_code"] is None
            and row["seed_key"] is None
        ))

        selected_users: list[tuple[int, str]] = []
        for row in conn.execute("SELECT id,username FROM users ORDER BY id"):
            user_id = int(row["id"])
            if not _TEST_USER.match(row["username"]):
                continue
            checks = (
                ("org_people", "user_id"),
                ("conversations", "user_id"),
                ("document_permissions", "owner_id"),
                ("data_sources", "owner_id"),
                ("analysis_cards", "user_id"),
            )
            if any(conn.execute(
                f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (user_id,)
            ).fetchone() for table, column in checks):
                continue
            if kb_ids:
                placeholders = _placeholders(kb_ids)
                non_test_kb = conn.execute(
                    f"SELECT 1 FROM kb_metadata WHERE owner_id=? "
                    f"AND kb_id NOT IN ({placeholders}) LIMIT 1",
                    (user_id, *kb_ids),
                ).fetchone()
            else:
                non_test_kb = conn.execute(
                    "SELECT 1 FROM kb_metadata WHERE owner_id=? LIMIT 1", (user_id,)
                ).fetchone()
            if non_test_kb is None:
                selected_users.append((user_id, row["username"]))

    return CleanupCandidates(
        user_ids=tuple(item[0] for item in selected_users),
        usernames=tuple(item[1] for item in selected_users),
        kb_ids=kb_ids,
    )


def apply_cleanup(db, expected: CleanupCandidates) -> dict[str, int]:
    """Revalidate candidates and delete them in one transaction."""
    current = find_candidates(db)
    if current != expected:
        raise ValueError("cleanup candidates changed; run dry-run again")

    with db.locked_transaction(immediate=True) as conn:
        for kb_id in expected.kb_ids:
            conn.execute(
                "DELETE FROM document_shares WHERE doc_id IN "
                "(SELECT id FROM document_permissions WHERE kb_id=?)",
                (kb_id,),
            )
            conn.execute("DELETE FROM document_permissions WHERE kb_id=?", (kb_id,))
            conn.execute("DELETE FROM kb_shares WHERE kb_id=?", (kb_id,))
            conn.execute("DELETE FROM kb_documents WHERE kb_id=?", (kb_id,))
            conn.execute("DELETE FROM kb_org_grants WHERE kb_id=?", (kb_id,))
            conn.execute("DELETE FROM kb_org_policies WHERE kb_id=?", (kb_id,))
            conn.execute("DELETE FROM kb_metadata WHERE kb_id=?", (kb_id,))
            conn.execute(
                "DELETE FROM org_audit_events WHERE target_type='knowledge_base' "
                "AND target_id=?",
                (kb_id,),
            )
        for user_id in expected.user_ids:
            conn.execute("DELETE FROM org_audit_events WHERE actor_id=?", (user_id,))
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))

    return {
        "users_deleted": len(expected.user_ids),
        "knowledge_bases_deleted": len(expected.kb_ids),
    }
