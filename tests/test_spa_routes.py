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


def test_organization_route_serves_frontend_entrypoint():
    client = TestClient(app)

    response = client.get("/organization", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert '<div id="app"></div>' in response.text


def test_wiki_route_serves_frontend_entrypoint():
    client = TestClient(app)

    response = client.get("/wiki", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert '<div id="app"></div>' in response.text


def test_wiki_detail_route_serves_frontend_entrypoint():
    client = TestClient(app)

    response = client.get("/wiki/42", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert '<div id="app"></div>' in response.text


