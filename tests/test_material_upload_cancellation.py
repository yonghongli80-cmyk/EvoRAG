"""Cancellation safety for synchronous material upload workers."""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers, UploadFile

from rag.kb_lifecycle import KBBusy, kb_mutation
from rag.user_db import UserDB


def _route_context(tmp_path: Path):
    import rag.materials.api as material_api

    db = UserDB(tmp_path / "users.db")
    db.create_material_schema()
    user_id = db.create_user("upload-worker", "Password-123!")
    user = {"id": user_id, "username": "upload-worker", "is_admin": False}
    versions_root = tmp_path / "versions"
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                user_db=db,
                document_versions_root=versions_root,
            )
        ),
        query_params={},
    )
    return material_api, db, user, request, versions_root


def _capture_stage(monkeypatch, material_api):
    stages: list[Path] = []
    stream = material_api.stream_upload_to_stage

    async def capture(*args, **kwargs):
        staged = await stream(*args, **kwargs)
        stages.append(staged.path)
        return staged

    monkeypatch.setattr(material_api, "stream_upload_to_stage", capture)
    return stages


def _watch_stage_cleanup(monkeypatch, stages: list[Path]):
    cleaned = threading.Event()
    unlink = Path.unlink

    def watched_unlink(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if stages and path == stages[0]:
            cleaned.set()
        return result

    monkeypatch.setattr(Path, "unlink", watched_unlink)
    return cleaned


def _kb_is_busy(db: UserDB, kb_id: str) -> bool:
    try:
        with kb_mutation(db, kb_id):
            return False
    except KBBusy:
        return True


def _uploaded_file(filename: str, payload: bytes, content_type: str) -> UploadFile:
    return UploadFile(
        filename=filename,
        file=io.BytesIO(payload),
        headers=Headers({"content-type": content_type}),
    )


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("child.txt", "archive child")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("filename", "payload", "content_type", "is_archive"),
    [
        ("ordinary.txt", b"ordinary material", "text/plain", False),
        ("bundle.zip", _zip_bytes(), "application/zip", True),
    ],
)
def test_cancelled_upload_keeps_stage_until_ordinary_or_archive_worker_finishes(
    tmp_path, monkeypatch, filename, payload, content_type, is_archive
):
    import rag.materials.archives as archives

    material_api, db, user, request, versions_root = _route_context(tmp_path)
    stages = _capture_stage(monkeypatch, material_api)
    entered = threading.Event()
    release = threading.Event()
    worker_operation_finished = threading.Event()
    cleaned = _watch_stage_cleanup(monkeypatch, stages)
    caller_thread = threading.get_ident()
    operation_threads: list[int] = []
    audit_events: list[tuple[str, int]] = []

    real_audit = material_api._audit_org_material

    def record_audit(*args, **kwargs):
        audit_events.append((args[2], threading.get_ident()))
        return real_audit(*args, **kwargs)

    monkeypatch.setattr(material_api, "_audit_org_material", record_audit)

    if is_archive:
        real_copyfile = archives.shutil.copyfile

        def blocked_copyfile(source, destination, *args, **kwargs):
            if Path(source) in stages:
                operation_threads.append(threading.get_ident())
                entered.set()
                try:
                    assert release.wait(timeout=5)
                    return real_copyfile(source, destination, *args, **kwargs)
                finally:
                    worker_operation_finished.set()
            return real_copyfile(source, destination, *args, **kwargs)

        monkeypatch.setattr(archives.shutil, "copyfile", blocked_copyfile)
    else:
        real_promote = material_api.promote_staged_version

        def blocked_promote(*args, **kwargs):
            operation_threads.append(threading.get_ident())
            entered.set()
            try:
                assert release.wait(timeout=5)
                return real_promote(*args, **kwargs)
            finally:
                worker_operation_finished.set()

        monkeypatch.setattr(material_api, "promote_staged_version", blocked_promote)

    async def cancel_during_worker():
        caller = asyncio.create_task(
            material_api.upload_material(
                request,
                _uploaded_file(filename, payload, content_type),
                user=user,
            )
        )
        assert await asyncio.to_thread(entered.wait, 5)
        assert len(stages) == 1
        stage = stages[0]
        assert stage.is_file()

        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        stage_remained_after_cancellation = stage.is_file()
        gate_remained_busy = await asyncio.to_thread(_kb_is_busy, db, "rag_docs")
        release.set()

        assert await asyncio.to_thread(worker_operation_finished.wait, 5)
        assert await asyncio.to_thread(cleaned.wait, 5)
        return stage, stage_remained_after_cancellation, gate_remained_busy

    try:
        stage, stage_remained, gate_remained_busy = asyncio.run(cancel_during_worker())

        assert stage_remained
        assert gate_remained_busy
        assert not stage.exists()
        assert len(operation_threads) == 1
        assert len(audit_events) == 1
        assert audit_events[0][1] == operation_threads[0]
        assert audit_events[0][1] != caller_thread

        if is_archive:
            manifest = db.get_material_archive_manifest(1)
            assert manifest is not None
            assert Path(manifest["source_path"]).is_file()
            assert audit_events[0][0] == "archive_uploaded"
        else:
            document = db.get_document_permission(filename, "rag_docs")
            assert document is not None
            versions = db.list_document_versions(document["id"])
            assert len(versions) == 1
            assert Path(versions[0]["file_path"]).is_file()
            assert db.get_index_job(1) is not None
            assert audit_events[0][0] == "document_uploaded"
    finally:
        release.set()
        db.close()


def test_cancelled_upload_worker_runs_even_when_queued_behind_single_worker(tmp_path, monkeypatch):
    material_api, db, user, request, _versions_root = _route_context(tmp_path)
    stages = _capture_stage(monkeypatch, material_api)
    cleaned = _watch_stage_cleanup(monkeypatch, stages)
    blocker_entered = threading.Event()
    blocker_release = threading.Event()
    promotion_entered = threading.Event()
    real_promote = material_api.promote_staged_version

    def occupy_executor():
        blocker_entered.set()
        assert blocker_release.wait(timeout=5)

    def track_promotion(*args, **kwargs):
        promotion_entered.set()
        return real_promote(*args, **kwargs)

    monkeypatch.setattr(material_api, "promote_staged_version", track_promotion)

    async def cancel_queued_worker():
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        blocker = loop.run_in_executor(None, occupy_executor)
        assert blocker_entered.wait(timeout=5)

        submitted = asyncio.Event()

        async def submit_and_signal(func, /, *args, **kwargs):
            future = loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
            submitted.set()
            return await future

        monkeypatch.setattr(asyncio, "to_thread", submit_and_signal)
        caller = asyncio.create_task(
            material_api.upload_material(
                request,
                _uploaded_file("queued.txt", b"queued material", "text/plain"),
                user=user,
            )
        )
        await submitted.wait()
        assert len(stages) == 1
        stage = stages[0]
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        stage_remained_after_cancellation = stage.is_file()
        blocker_release.set()
        promotion_ran = promotion_entered.wait(timeout=5)
        worker_cleaned_stage = cleaned.wait(timeout=5) if promotion_ran else False
        await blocker
        return (
            stage,
            stage_remained_after_cancellation,
            promotion_ran,
            worker_cleaned_stage,
        )

    try:
        (
            stage,
            stage_remained,
            promotion_ran,
            worker_cleaned_stage,
        ) = asyncio.run(cancel_queued_worker())
        assert stage_remained
        assert promotion_ran
        assert worker_cleaned_stage
        assert not stage.exists()
        document = db.get_document_permission("queued.txt", "rag_docs")
        assert document is not None
        assert len(db.list_document_versions(document["id"])) == 1
    finally:
        blocker_release.set()
        db.close()


def test_cancelled_upload_worker_failure_is_logged_without_unhandled_task_error(tmp_path, monkeypatch, caplog):
    material_api, db, user, request, _versions_root = _route_context(tmp_path)
    stages = _capture_stage(monkeypatch, material_api)
    cleaned = _watch_stage_cleanup(monkeypatch, stages)
    entered = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    worker_failure_observed = threading.Event()
    loop_errors: list[dict] = []
    worker_logger = logging.getLogger("rag.materials.upload_execution")

    class WorkerFailureHandler(logging.Handler):
        def emit(self, record):
            if "material_upload_worker_failed_after_cancellation" in record.getMessage():
                worker_failure_observed.set()

    failure_handler = WorkerFailureHandler()
    worker_logger.addHandler(failure_handler)

    def fail_after_release(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(timeout=5)
            raise RuntimeError("private worker failure details")
        finally:
            worker_finished.set()

    monkeypatch.setattr(material_api, "promote_staged_version", fail_after_release)

    async def cancel_failing_worker():
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            caller = asyncio.create_task(
                material_api.upload_material(
                    request,
                    _uploaded_file("failure.txt", b"failure", "text/plain"),
                    user=user,
                )
            )
            assert await asyncio.to_thread(entered.wait, 5)
            stage = stages[0]
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            stage_remained_after_cancellation = stage.is_file()
            release.set()
            assert await asyncio.to_thread(worker_finished.wait, 5)
            assert await asyncio.to_thread(cleaned.wait, 5)
            assert await asyncio.to_thread(worker_failure_observed.wait, 5)
            return stage_remained_after_cancellation
        finally:
            loop.set_exception_handler(previous_handler)

    try:
        stage_remained = asyncio.run(cancel_failing_worker())
        assert stage_remained
        assert not stages[0].exists()
        assert "material_upload_worker_failed_after_cancellation" in caplog.text
        assert "exception_type=RuntimeError" in caplog.text
        assert "private worker failure details" not in caplog.text
        assert loop_errors == []
        assert db.get_document_permission("failure.txt", "rag_docs") is not None
    finally:
        release.set()
        worker_logger.removeHandler(failure_handler)
        db.close()
