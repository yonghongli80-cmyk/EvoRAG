def test_files_returns_logical_document_id_for_an_authorized_viewer(
    isolated_api, make_user
):
    owner, owner_headers = make_user("owner")
    document_id = isolated_api.app.state.user_db.create_document_permission(
        "visible.txt", "rag_docs", owner["id"]
    )
    upload_dir = isolated_api.app.state.upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "visible.txt").write_text("comparison source", encoding="utf-8")

    response = isolated_api.get("/files", headers=owner_headers)

    assert response.status_code == 200
    files = response.json()["files"]
    assert len(files) == 1
    assert files[0]["document_id"] == document_id
