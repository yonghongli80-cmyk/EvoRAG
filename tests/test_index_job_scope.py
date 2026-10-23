from rag.user_db import UserDB
from rag.wiki.indexer import enqueue_wiki_reindex_job


def test_wiki_and_material_jobs_share_an_id_but_not_a_scope(tmp_path):
    db = UserDB(str(tmp_path / "jobs.db"))
    try:
        assert enqueue_wiki_reindex_job(db, page_id=7, revision_id=1) is True
        db.enqueue_latest_index_job(7, 1, "hash", 0)

        with db.locked_read() as conn:
            rows = conn.execute(
                "SELECT job_scope,job_type,status FROM document_index_jobs ORDER BY id"
            ).fetchall()
        assert [(row["job_scope"], row["job_type"], row["status"]) for row in rows] == [
            ("wiki", "wiki_reindex", "queued"),
            ("material", "material_index", "queued"),
        ]
    finally:
        db.close()
