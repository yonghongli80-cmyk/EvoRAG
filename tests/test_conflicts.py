import pytest

from rag.reliability.conflicts import ConflictClaim, detect_conflicts, persist_conflict, resolve_conflict
from rag.user_db import UserDB


def test_overlapping_current_claims_with_different_values_conflict():
    claims = [
        ConflictClaim(
            subject="年假",
            attribute="天数",
            value="5",
            unit="days",
            scope="all",
            effective_at=None,
            citation_id=1,
            acl_checked=True,
        ),
        ConflictClaim(
            subject="年假",
            attribute="天数",
            value="7",
            unit="days",
            scope="all",
            effective_at=None,
            citation_id=2,
            acl_checked=True,
        ),
    ]

    result = detect_conflicts(claims)

    assert len(result) == 1
    assert result[0].citation_ids == (1, 2)


def test_conflict_resolution_requires_admin_or_common_document_owner(tmp_path):
    db = UserDB(str(tmp_path / "conflicts.db"))
    alice_id = db.create_user("alice", "Password-123!")
    bob_id = db.create_user("bob", "Password-123!")
    admin_id = db.create_user("admin", "Password-123!")
    db.set_user_admin(admin_id, True)
    first_document_id = db.create_document_permission("a.pdf", "kb", alice_id)
    second_document_id = db.create_document_permission("b.pdf", "kb", bob_id)
    conflict = db.create_knowledge_conflict(
        active_key="annual-leave-days:a-b",
        subject_key="annual leave",
        attribute_key="days",
        unit_key="days",
        scope_key="all",
        citation_ids=(1, 2),
        document_ids=(first_document_id, second_document_id),
        document_version_ids=(11, 12),
    )

    with pytest.raises(PermissionError, match="not authorized"):
        resolve_conflict(db, conflict["id"], {"id": alice_id, "is_admin": False}, "resolved", "")

    resolved = resolve_conflict(
        db,
        conflict["id"],
        {"id": admin_id, "is_admin": True},
        "resolved",
        "owner confirmed the current policy",
    )

    assert resolved["status"] == "resolved"
    assert resolved["resolved_by"] == admin_id
    events = db.list_knowledge_conflict_events(conflict["id"])
    assert [
        (event["actor_id"], event["old_status"], event["new_status"], event["note"])
        for event in events
    ] == [
        (admin_id, "open", "resolved", "owner confirmed the current policy")
    ]


def test_persist_conflict_deduplicates_normalized_evidence_versions(tmp_path):
    db = UserDB(str(tmp_path / "conflicts.db"))
    owner_id = db.create_user("owner", "Password-123!")
    first_document_id = db.create_document_permission("a.pdf", "kb", owner_id)
    second_document_id = db.create_document_permission("b.pdf", "kb", owner_id)
    candidate = detect_conflicts(
        [
            ConflictClaim(
                subject=" 年假 ",
                attribute="天数",
                value="5",
                unit="days",
                scope="all",
                effective_at=None,
                citation_id=8,
                acl_checked=True,
                document_id=first_document_id,
                document_version_id=11,
            ),
            ConflictClaim(
                subject="年假",
                attribute="天数",
                value="7",
                unit="days",
                scope="all",
                effective_at=None,
                citation_id=4,
                acl_checked=True,
                document_id=second_document_id,
                document_version_id=12,
            ),
        ]
    )[0]

    first = persist_conflict(db, candidate)
    second = persist_conflict(db, candidate)

    assert first["id"] == second["id"]
    assert first["citation_ids"] == (4, 8)
    assert first["document_ids"] == tuple(sorted((first_document_id, second_document_id)))
    assert first["document_version_ids"] == (11, 12)


def test_conflict_detection_rejects_acl_unchecked_claims():
    unchecked = ConflictClaim(
        subject="年假",
        attribute="天数",
        value="5",
        unit="days",
        scope="all",
        effective_at=None,
        citation_id=1,
        acl_checked=False,
    )

    with pytest.raises(ValueError, match="acl_checked"):
        detect_conflicts([unchecked])
