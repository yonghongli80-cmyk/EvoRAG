"""Lifecycle owner for the API-owned material indexing worker.

The worker used to live inside ``rag/api.py`` with three module globals and a
stop event; extracting it keeps the application assembler about routing and
startup ordering, and gives the worker one explicit owner. The public functions
``_start_material_worker``/``_stop_material_worker`` remain as thin aliases for
backwards compatibility with scripts/tests that patch them.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

from config import settings

logger = logging.getLogger(__name__)


class MaterialWorkerSupervisor:
    """Start/stop the one material worker owned by the API process."""

    def __init__(self, db, versions_root: Callable[[], object]) -> None:
        self._db = db
        self._versions_root = versions_root
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker = None

    def start(self) -> None:
        if not settings.material_worker_enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return

        from rag.embedder import embed
        from rag.materials.formats import build_default_format_registry
        from rag.materials.worker import MaterialWorker, WorkerServices

        worker = MaterialWorker(
            WorkerServices(
                db=self._db,
                versions_root=self._versions_root(),
                registry=build_default_format_registry(),
                embedder=embed,
                max_embedding_concurrency=settings.material_embedding_slots,
            ),
            worker_id=f"api-material-{uuid.uuid4().hex[:8]}",
            lease_seconds=settings.material_worker_lease_seconds,
            max_attempts=settings.material_job_max_attempts,
        )
        self._stop.clear()

        def _consume() -> None:
            while not self._stop.is_set():
                try:
                    worker.run(batch=settings.material_worker_count)
                except Exception:
                    logger.exception("material index worker polling iteration failed")
                self._stop.wait(1.0)

        self._worker = worker
        self._thread = threading.Thread(
            target=_consume, name="material-index-worker", daemon=True
        )
        self._thread.start()
        logger.info("material index worker started")

    def stop(self) -> bool:
        """Stop the worker before shared vector resources close; idempotent."""
        worker = self._worker
        thread = self._thread
        self._stop.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        if thread is not None and thread.is_alive():
            logger.error(
                "material index worker did not stop before shutdown; "
                "preserving shared resources"
            )
            return False
        if worker is not None:
            worker.shutdown()
        self._worker = None
        self._thread = None
        return True
