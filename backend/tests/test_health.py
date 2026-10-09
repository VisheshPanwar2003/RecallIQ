from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Welcome to RecallIQ API"}


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "recalliq-backend"}


def test_swagger_ui():
    response = client.get("/docs")
    assert response.status_code == 200
    assert "swagger-ui" in response.text


def test_openapi_schema():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "RecallIQ API"
    assert "get" in schema["paths"]["/"]
    assert "get" in schema["paths"]["/health"]
