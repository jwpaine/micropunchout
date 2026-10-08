import pytest

from tests.conftest import HTML, login


def test_home_redirects_to_login_when_anonymous(client):
    r = client.get("/", headers=HTML, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_home_returns_401_for_non_html(client):
    assert client.get("/", headers={"accept": "application/json"}).status_code == 401


def test_login_success_then_home(client):
    r = login(client)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    home = client.get("/", headers=HTML)
    assert home.status_code == 200
    assert "Welcome, admin" in home.text


def test_login_wrong_password(client):
    r = login(client, password="nope")
    assert r.status_code == 401
    assert "Invalid username or password" in r.text
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 303


def test_login_unknown_user(client):
    assert login(client, username="ghost").status_code == 401


def test_logout_clears_session(client):
    login(client)
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 303


@pytest.mark.parametrize("bad", ["https://evil.com", "//evil.com", "/\\evil.com", "evil"])
def test_login_rejects_open_redirect(client, bad):
    assert login(client, next=bad).headers["location"] == "/"


def test_login_honors_safe_next(client):
    assert login(client, next="/settings").headers["location"] == "/settings"
