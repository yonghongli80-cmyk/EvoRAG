"""Multipart upload smoke test."""


def test_upload_smoke_not_500(
    tmp_path, isolated_api, make_user, mock_embedding_indexing
):
    """POST /upload parses multipart data and completes the ingest boundary."""
    _, headers = make_user("upload-smoke")
    tmp_file = tmp_path / "hello.txt"
    tmp_file.write_bytes(b"hello rag upload")

    with tmp_file.open("rb") as file_obj:
        response = isolated_api.post(
            "/upload",
            headers=headers,
            files={"file": ("hello.txt", file_obj, "text/plain")},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "created"
