from rag.spreadsheets.audit import compute_risk_flags


def test_risk_flags_are_deterministic_for_public_bulk_clearing_and_frequency():
    changes = [
        {"old_value_json": '"value"', "new_value_json": "null"}
        for _ in range(101)
    ]

    flags = compute_risk_flags(
        "edit",
        changes,
        actor_history=6,
        document={"scope": "public"},
    )

    assert flags == (
        "bulk_change",
        "mass_clear",
        "public_document_edit",
        "rapid_repeated_saves",
    )


def test_restore_and_rejected_attempt_risks_are_explicit():
    assert compute_risk_flags(
        "rollback", (), 0, {"non_previous_restore": True}
    ) == ("non_previous_restore",)
    assert compute_risk_flags(
        "edit", (), 0, {"rejected_reason": "formula_edit_forbidden"}
    ) == ("rejected_formula_edit",)
    assert compute_risk_flags(
        "edit", (), 0, {"rejected_reason": "protected_cell"}
    ) == ("rejected_protected_cell_edit",)
