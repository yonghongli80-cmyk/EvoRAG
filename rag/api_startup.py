"""Startup migrations and incremental indexing for the API application.

This module deliberately imports no application modules at import time. The
API module is passed to each implementation so existing monkeypatch points
remain dynamic without a reverse import or a snapshot of application state.
"""

from __future__ import annotations

from pathlib import Path


def _run_document_version_migration(api) -> None:
    """Create version-1 rows for documents created before versioning."""
    from rag.document_versions import migrate_existing_document_versions

    try:
        report = migrate_existing_document_versions(api.user_db, api._versions_root(), api.DATA_DIR)
        if report.migrated or report.missing:
            api.logger.info(
                "document version migration: migrated=%s missing=%s unchanged=%s",
                report.migrated,
                report.missing,
                report.unchanged,
            )
    except Exception:
        api.logger.exception("document version migration failed")


def _run_material_metadata_migration(api) -> None:
    """Backfill material FTS/metadata rows without re-embedding content."""
    from rag.materials.migration import migrate_material_metadata

    try:
        api.user_db.create_material_schema()
        report = migrate_material_metadata(api.user_db)
        if report.migrated_documents:
            api.logger.info(
                "material metadata migration: migrated=%s missing=%s",
                report.migrated_documents,
                report.missing_sources,
            )
    except Exception:
        api.logger.exception("material metadata migration failed")


def _run_spreadsheet_migration(api) -> None:
    from rag.spreadsheets.migration import migrate_existing_spreadsheets

    try:
        report = migrate_existing_spreadsheets(
            api.user_db,
            api.Path(
                getattr(
                    api.app.state,
                    "structured_datasets_root",
                    api.settings.structured_datasets_root,
                )
            ),
        )
        if report.migrated or report.failed:
            api.logger.info(
                "spreadsheet migration: migrated=%s failed=%s",
                report.migrated,
                report.failed,
            )
    except Exception:
        api.logger.exception("spreadsheet migration failed")


def _register_repo_document_version(api, doc_name: str, source_path: Path) -> bool:
    """Register a repository fixture as an immutable document version when missing."""
    from rag.vector_store import delete_unversioned_points

    permission = api.user_db.get_document_permission(doc_name, "rag_docs")
    if permission is None or permission.get("current_version_id"):
        return False
    actor = api.user_db.ensure_development_user()
    manager = api.KnowledgeBaseManager()
    source = api.UploadDocumentSource.from_bytes(doc_name, source_path.read_bytes())
    api.ingest_document_version(
        api.user_db,
        manager,
        source,
        "rag_docs",
        actor,
        "overwrite",
        api.Path(api.settings.document_versions_root),
    )
    delete_unversioned_points("rag_docs", doc_name)
    return True


def _run_legacy_kb_migration(api) -> None:
    """Assign every legacy knowledge base an explicit organization policy.

    The 2026-08-20 plan (§7) retires the personal KB ACL during migration: a
    legacy knowledge base becomes ``unassigned`` and stays administrator-only
    until an administrator explicitly assigns an organization. Running this on
    every start makes the fail-closed guard in ``require_kb_view`` deterministic
    instead of depending on a manual ``seed.py`` invocation.
    """
    from rag.org_auth.migration import migrate_legacy_knowledge_bases

    try:
        report = migrate_legacy_knowledge_bases(api.user_db)
        api.logger.info(
            "legacy knowledge base migration: migrated=%s remaining=%s",
            report.migrated,
            report.unmigrated,
        )
        if report.unmigrated:
            api.logger.warning(
                "legacy knowledge base migration incomplete: %s knowledge base(s) "
                "still lack an organization policy and stay owner/administrator-only",
                report.unmigrated,
            )
    except Exception:
        api.logger.exception("legacy knowledge base migration failed")


def _run_staged_version_migration(api) -> None:
    """Promote committed version files left behind under ``staging/``.

    Older ``POST /materials/upload`` rows stored ``document_versions.file_path``
    in the transient ``staging`` directory, which backups exclude, so the
    authoritative backup failed with ``referenced_file_missing`` forever. The
    migration is idempotent and must run before the backup scheduler can open a
    window; a failure is logged and never blocks startup.
    """
    from rag.document_versions import migrate_staged_document_versions

    try:
        report = migrate_staged_document_versions(api.user_db, api._versions_root())
        if report.migrated or report.missing or report.conflicts or report.skipped:
            api.logger.info(
                "staged version migration: migrated=%s unchanged=%s missing=%s skipped=%s conflicts=%s",
                report.migrated,
                report.unchanged,
                report.missing,
                report.skipped,
                report.conflicts,
            )
    except Exception:
        api.logger.exception("staged version migration failed")


def auto_index_on_startup(api) -> None:
    """Index changed upload files and finish the API-owned worker startup."""
    api.log_security_mode()
    api._run_document_version_migration()
    api._run_material_metadata_migration()
    api._run_spreadsheet_migration()
    if not api.DATA_DIR.is_dir():
        api._start_material_worker()
        return

    from rag.folder_indexer import (
        compute_file_hash,
        diff_index,
        index_folder,
        load_index_state,
        save_index_state,
    )

    state_path = str(api.DATA_DIR.parent / "index_state.json")
    stored_state = load_index_state(state_path)

    current_hashes = {}
    for fpath in api.DATA_DIR.iterdir():
        if fpath.is_file() and fpath.suffix.lower() in api.SUPPORTED_EXTENSIONS:
            current_hashes[fpath.name] = compute_file_hash(str(fpath))

    added, modified, deleted = diff_index(current_hashes, stored_state)
    changed = added + modified

    collection_check_failed = False
    try:
        from rag.vector_store import COLLECTION_NAME, _get_client

        collection_available = _get_client().collection_exists(COLLECTION_NAME)
    except Exception as error:
        api.logger.warning("无法确认向量索引状态，将跳过本次自动索引: %s", error)
        collection_check_failed = True
        collection_available = None
    if collection_available is False:
        api.logger.warning("向量索引集合不存在，将重建索引")
        changed = list(current_hashes)

    def _init_admin():
        admin_username = api.settings.init_admin_username
        if not admin_username:
            return
        target = api.user_db.get_user_by_username(admin_username)
        if target and not target.get("is_admin"):
            api.user_db.set_user_admin(target["id"], True)
            api.logger.info("已将用户 %s 设置为管理员", admin_username)

    system_owner_id = 0

    def _sync_repo_files():
        import shutil

        repo_upload = api.Path("/app/repo_upload")
        if not repo_upload.is_dir():
            repo_upload = api.Path(api.__file__).resolve().parent.parent / "data" / "upload"
        if not repo_upload.is_dir():
            return

        manifest_path = repo_upload / ".repo_files"
        repo_file_names = set()
        if manifest_path.is_file():
            for line in manifest_path.read_text(encoding="utf-8").splitlines():
                name = line.strip()
                if name:
                    repo_file_names.add(name)
        else:
            for fpath in repo_upload.iterdir():
                if fpath.is_file() and fpath.suffix.lower() in api.SUPPORTED_EXTENSIONS:
                    repo_file_names.add(fpath.name)

        if not api.DATA_DIR.is_dir():
            api.DATA_DIR.mkdir(parents=True, exist_ok=True)
        for filename in repo_file_names:
            source = repo_upload / filename
            if not source.is_file():
                continue
            destination = api.DATA_DIR / filename
            if not destination.exists():
                shutil.copy2(str(source), str(destination))
                api.logger.info("同步仓库文件: %s", filename)
            existing = api.user_db.get_document_permission(filename, "rag_docs")
            if not existing:
                api.user_db.create_document_permission(
                    filename,
                    "rag_docs",
                    system_owner_id,
                    is_public=True,
                    protected=True,
                )
                api.logger.info("创建受保护权限: %s (owner=system)", filename)
            elif existing.get("owner_id") == system_owner_id and (
                not existing.get("protected") or existing.get("scope") != "public"
            ):
                with api.user_db.locked_transaction() as connection:
                    connection.execute(
                        "UPDATE document_permissions SET protected = 1, is_public = 1, scope = 'public' WHERE id = ?",
                        (existing["id"],),
                    )
                api.logger.info("更新为受保护+公开: %s", filename)

            try:
                if api._register_repo_document_version(filename, destination):
                    api.logger.info("已登记文档版本: %s", filename)
            except Exception:
                api.logger.exception("仓库文件版本登记失败: %s", filename)

    if collection_check_failed:
        try:
            _init_admin()
            _sync_repo_files()
        except Exception as error:
            api.logger.warning("管理员初始化失败: %s", error)
        return

    if not changed and not deleted:
        api.logger.info("索引无需更新（%d 文件）", len(current_hashes))
        with api._pipeline_lock.write():
            api.set_pipeline(api._get_or_create_pipeline("rag_docs"))
        _init_admin()
        _sync_repo_files()
        api._start_material_worker()
        return

    api.logger.info("索引变化: %d 新增, %d 修改, %d 删除", len(added), len(modified), len(deleted))

    try:
        if changed:
            stats = index_folder(str(api.DATA_DIR))
            api.logger.info("索引完成: %d 文件, %d 分块", stats["files"], stats["chunks"])

        new_state = {"files": {}}
        for name, file_hash in current_hashes.items():
            new_state["files"][name] = {"hash": file_hash}
        save_index_state(state_path, new_state)

        with api._pipeline_lock.write():
            api.set_pipeline(api._get_or_create_pipeline("rag_docs"))
    except Exception as error:
        api.logger.warning("启动索引失败: %s", error)

    try:
        _init_admin()
        _sync_repo_files()
    except Exception as error:
        api.logger.warning("管理员初始化失败: %s", error)
    api._start_material_worker()
