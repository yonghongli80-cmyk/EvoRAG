from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from config import settings
from rag.backups.models import BackupError, context_from_settings
from rag.backups.service import create_if_due

logger = logging.getLogger(__name__)


class BackupScheduler:
    def __init__(
        self,
        run_once: Callable[[], object],
        check_interval_seconds: float,
    ):
        self._run_once = run_once
        self._check_interval_seconds = check_interval_seconds
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._state_lock:
            if self.is_alive:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="rag-weekly-backup",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._state_lock:
            thread = self._thread
            self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._state_lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_once()
            except BackupError as error:
                logger.warning("backup_scheduler_run_failed code=%s", error.code)
            except Exception:
                logger.error("backup_scheduler_unexpected_failure")
            if self._stop_event.wait(self._check_interval_seconds):
                break


_scheduler: BackupScheduler | None = None
_scheduler_lock = threading.Lock()


def start_backup_scheduler() -> BackupScheduler | None:
    global _scheduler
    if not settings.backup_enabled:
        return None
    with _scheduler_lock:
        if _scheduler is not None and _scheduler.is_alive:
            return _scheduler
        context = context_from_settings(settings)
        _scheduler = BackupScheduler(
            run_once=lambda: create_if_due(context, "scheduled"),
            check_interval_seconds=context.check_interval_seconds,
        )
        _scheduler.start()
        return _scheduler


def stop_backup_scheduler() -> None:
    global _scheduler
    with _scheduler_lock:
        scheduler = _scheduler
        _scheduler = None
    if scheduler is not None:
        scheduler.stop()


def backup_scheduler_is_alive() -> bool:
    scheduler = _scheduler
    return scheduler is not None and scheduler.is_alive
