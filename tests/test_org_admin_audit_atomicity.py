import sqlite3

import pytest


def _state(db):
    with db.locked_read() as conn:
        return {
            "policy": [
                tuple(r)
                for r in conn.execute(
                    "SELECT kb_id,org_code,kb_type,sensitivity,active FROM kb_org_policies ORDER BY kb_id"
                )
            ],
            "people": [
                tuple(r)
                for r in conn.execute(
                    "SELECT person_code,active,job_title,security_version FROM org_people ORDER BY person_code"
                )
            ],
            "grants": [
                tuple(r)
                for r in conn.execute(
                    "SELECT id,kb_id,subject_type,subject_id,role,effect,active FROM kb_org_grants ORDER BY id"
                )
            ],
            "audit": [
                tuple(r) for r in conn.execute("SELECT action,target_id,result FROM org_audit_events ORDER BY id")
            ],
        }


@pytest.mark.parametrize("command", ["policy", "person", "create_grant", "revoke_grant"])
@pytest.mark.parametrize("failure", ["statement", "deferred_commit"])
def test_admin_command_and_audit_roll_back_together_and_retry(isolated_api, make_user, command, failure):
    admin, headers = make_user("atomic_admin", is_admin=True)
    member, _ = make_user("atomic_member")
    db = isolated_api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('TEAM','Team','team')")
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P1',?,'Member','Old')",
            (member["id"],),
        )
        conn.execute("INSERT INTO org_memberships VALUES('P1','TEAM',1)")
        conn.execute("INSERT INTO kb_metadata(kb_id,name) VALUES('kb','Knowledge')")
        conn.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('kb',NULL,'unassigned')")
        if command == "revoke_grant":
            conn.execute(
                "INSERT INTO kb_org_grants(kb_id,subject_type,subject_id,role,effect,granted_by) VALUES('kb','user',?,'read','allow',?)",
                (str(member["id"]), admin["id"]),
            )
        if failure == "statement":
            conn.execute(
                "CREATE TRIGGER fail_audit BEFORE INSERT ON org_audit_events BEGIN SELECT RAISE(ABORT, 'audit failure'); END"
            )
        else:
            conn.execute("CREATE TABLE audit_fk_parent (id INTEGER PRIMARY KEY)")
            conn.execute(
                "CREATE TABLE audit_fk_child (parent_id INTEGER REFERENCES audit_fk_parent(id) DEFERRABLE INITIALLY DEFERRED)"
            )
            conn.execute(
                "CREATE TRIGGER fail_audit AFTER INSERT ON org_audit_events BEGIN INSERT INTO audit_fk_child VALUES(999); END"
            )

    def invoke():
        if command == "policy":
            return isolated_api.put(
                "/organization/knowledge-bases/kb/policy",
                headers=headers,
                json={"org_code": "TEAM", "kb_type": "organization"},
            )
        if command == "person":
            return isolated_api.patch(
                "/organization/people/P1", headers=headers, json={"active": False, "job_title": "New"}
            )
        if command == "create_grant":
            return isolated_api.post(
                "/organization/grants",
                headers=headers,
                json={
                    "kb_id": "kb",
                    "subject_type": "user",
                    "subject_id": str(member["id"]),
                    "role": "read",
                    "effect": "allow",
                },
            )
        return isolated_api.delete("/organization/grants/1", headers=headers)

    before = _state(db)
    with pytest.raises(sqlite3.IntegrityError):
        invoke()
    assert _state(db) == before
    with db.locked_transaction() as conn:
        conn.execute("DROP TRIGGER fail_audit")
        if failure == "deferred_commit":
            assert conn.execute("SELECT COUNT(*) FROM audit_fk_child").fetchone()[0] == 0

    response = invoke()
    assert response.status_code == 200
    after = _state(db)
    action = {
        "policy": "kb_policy_assigned",
        "person": "person_updated",
        "create_grant": "grant_created",
        "revoke_grant": "grant_revoked",
    }[command]
    assert after["audit"] == [(action, "P1" if command == "person" else "kb", "success")]
    if command == "policy":
        assert after["policy"] == [("kb", "TEAM", "organization", None, 1)]
        assert after["people"] == before["people"] and after["grants"] == before["grants"]
    elif command == "person":
        assert after["people"] == [("P1", 0, "New", 2)]
        assert after["policy"] == before["policy"] and after["grants"] == before["grants"]
    else:
        assert after["people"] == [("P1", 1, "Old", 2)]
        assert after["policy"] == before["policy"]
        assert after["grants"] == [
            (1, "kb", "user", str(member["id"]), "read", "allow", int(command == "create_grant"))
        ]
