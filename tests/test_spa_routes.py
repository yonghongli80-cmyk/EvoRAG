from fastapi.testclient import TestClient

from rag.api import app


def test_operations_route_serves_frontend_entrypoint():
    client = TestClient(app)

    response = client.get("/operations", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert '<div id="app"></div>' in response.text


def test_spreadsheet_deep_link_serves_frontend_entrypoint():
    client = TestClient(app)

    response = client.get(
        "/documents/13/versions/45/spreadsheet",
        headers={"accept": "text/html"},
    )

    assert response.status_code == 200
    assert '<div id="app"></div>' in response.text
