from contextlib import contextmanager

from rag.org_auth.repository import OrgRepository


@contextmanager
def authorization_read_snapshot(db):
    """Bind one authorization call to its thread-local reader transaction."""
    with db.locked_read() as conn:
        owns_transaction = not conn.in_transaction
        if owns_transaction:
            conn.execute("BEGIN")
        try:
            yield conn, OrgRepository(db, read_connection=conn)
        finally:
            if owns_transaction and conn.in_transaction:
                conn.rollback()
