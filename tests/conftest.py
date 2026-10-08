import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

HTML = {"accept": "text/html"}


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(secret_key="test", database_path=str(tmp_path / "test.db")))
    with TestClient(app) as c:  # the context manager runs startup, which creates the schema
        yield c


def login(client, username="admin", password="password123", next="/"):
    return client.post(
        "/login",
        data={"username": username, "password": password, "next": next},
        follow_redirects=False,
    )
