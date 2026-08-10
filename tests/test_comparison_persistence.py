from rag.reliability.comparison import (
    ComparisonCellDraft,
    ComparisonCitationDraft,
    ComparisonExecutionResult,
    ComparisonRequest,
    ComparisonRowDraft,
)
from rag.user_db import UserDB


def _execution(document_id: int, version_id: int) -> ComparisonExecutionResult:
    citation = ComparisonCitationDraft(
        document_id=document_id,
        document_version_id=version_id,
        chunk_index=0,
        section_title="Leave",
        text_preview="five days",
        rank=0,
    )
    return ComparisonExecutionResult(
        rows=(ComparisonRowDraft(
            dimension="leave",
            values={document_id: ComparisonCellDraft(
                status="supported", text="five days", citations=(citation,)
            )},
        ),),
        partial=False,
    )


def _configured_db(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    owner_id = db.create_user("owner", "Password-123!")
    owner = db.get_user_by_id(owner_id)
    document_id = db.create_document_permission("policy.pdf", "rag_docs", owner_id)
    return db, owner, document_id


def test_persisted_comparison_uses_independent_citation_rows(tmp_path):
    db, owner, document_id = _configured_db(tmp_path)
    request = ComparisonRequest(question="leave", document_ids=(document_id,))

    stored = db.persist_document_comparison(owner["id"], request, _execution(document_id, 11))

    assert stored.comparison_id > 0
    assert stored.rows[0].values[document_id].citation_ids
    assert db.get_answer_citations(stored.comparison_id) == []


def test_only_owner_or_admin_reads_persisted_comparison(tmp_path):
    db, owner, document_id = _configured_db(tmp_path)
    other_id = db.create_user("other", "Password-123!")
    admin_id = db.create_user("admin", "Password-123!")
    stored = db.persist_document_comparison(
        owner["id"],
        ComparisonRequest(question="leave", document_ids=(document_id,)),
        _execution(document_id, 11),
    )

    assert db.get_document_comparison(stored.comparison_id, other_id, False) is None
    assert db.get_document_comparison(stored.comparison_id, admin_id, True) is not None
