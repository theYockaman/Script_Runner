"""Login, session cookies, and route guarding. Credentials come from tests/conftest.py."""
import re
import time

import pytest
from fastapi.testclient import TestClient

from app import main as app_main

USER, PASSWORD = "tester", "testpass"


@pytest.fixture
def client():
    # A fresh client per test gives each test its own cookie jar.
    return TestClient(app_main.app)


def login(client, username=USER, password=PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def cookie_header(token):
    return {"Cookie": f"{app_main.SESSION_COOKIE}={token}"}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_parse_users_edge_cases():
    spec = " alice:pw1 , bob:p:w:2,\n carol:secret ,,invalid-entry, :nouser, nopass: , alice:pw-final"
    assert app_main.parse_users(spec) == {"alice": "pw-final", "bob": "p:w:2", "carol": "secret"}
    assert app_main.parse_users("") == {}
    assert app_main.parse_users(None) == {}


def test_token_round_trip_uses_cookie_safe_alphabet():
    token = app_main.make_session_token(USER)
    assert app_main.verify_session_token(token) == USER
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", token)  # never quoted by Set-Cookie


def test_token_rejections(monkeypatch):
    good = app_main.make_session_token(USER)
    encoded, issued, sig = good.split(".")
    assert app_main.verify_session_token(f"{encoded}.{issued}.{'0' * len(sig)}") is None  # tampered signature
    assert app_main.verify_session_token(f"{encoded}.{int(issued) + 1}.{sig}") is None  # tampered timestamp
    for bad in (None, "", "a.b", "!!!.x.y", "abc.notanint.ff", "é.1.2", "a.b.c.d"):
        assert app_main.verify_session_token(bad) is None
    monkeypatch.setattr(app_main, "SECRET_KEY", "another-key")
    assert app_main.verify_session_token(good) is None  # signed with a different key


def test_token_expiry_and_future_dates():
    now = int(time.time())
    assert app_main.verify_session_token(app_main.make_session_token(USER, issued=now - app_main.SESSION_MAX_AGE - 5)) is None
    assert app_main.verify_session_token(app_main.make_session_token(USER, issued=now + 60)) is None
    assert app_main.verify_session_token(app_main.make_session_token(USER, issued=now - 10)) == USER


def test_token_for_removed_user_is_rejected(monkeypatch):
    token = app_main.make_session_token(USER)
    monkeypatch.setattr(app_main, "USERS", {"someone-else": "x"})
    assert app_main.verify_session_token(token) is None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def test_status_unauthenticated(client):
    r = client.get("/api/auth/status")
    assert r.status_code == 200
    assert r.json() == {"authenticated": False, "username": None, "users_configured": True}


def test_wrong_password_and_unknown_user_look_identical(client):
    r1 = login(client, USER, "nope")
    r2 = login(client, "ghost", "nope")
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json() == {"detail": "Invalid username or password"}
    assert "set-cookie" not in r1.headers


def test_login_missing_field_is_422(client):
    assert client.post("/api/auth/login", json={"username": USER}).status_code == 422


def test_login_sets_cookie_and_status_reports_user(client):
    r = login(client)
    assert r.status_code == 200
    assert r.json() == {"username": USER}
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{app_main.SESSION_COOKIE}=")
    assert '"' not in cookie  # unquoted token
    lower = cookie.lower()
    assert "httponly" in lower and "samesite=lax" in lower and "path=/" in lower and "max-age=" in lower
    assert "secure" not in lower
    assert client.get("/api/auth/status").json() == {"authenticated": True, "username": USER, "users_configured": True}


def test_secure_flag_when_configured(client, monkeypatch):
    monkeypatch.setattr(app_main, "COOKIE_SECURE", True)
    assert "secure" in login(client).headers["set-cookie"].lower()


@pytest.mark.parametrize("path,status_when_logged_in", [
    ("/api/scripts", 200),
    ("/api/languages", 200),
    ("/api/scripts/999999", 404),
    ("/docs", 200),
    ("/openapi.json", 200),
])
def test_guarded_paths(client, path, status_when_logged_in):
    r = client.get(path)
    assert r.status_code == 401
    assert r.json() == {"detail": "Not authenticated"}
    assert login(client).status_code == 200
    assert client.get(path).status_code == status_when_logged_in


def test_write_endpoints_are_guarded(client):
    assert client.post("/api/scripts", json={"name": "x", "command": "echo hi"}).status_code == 401
    assert client.post("/api/scripts/1/run").status_code == 401
    assert client.put("/api/scripts/1", json={"enabled": False}).status_code == 401
    assert client.delete("/api/scripts/1").status_code == 401


@pytest.mark.parametrize("path", ["/", "/api/auth/status"])
def test_public_paths(client, path):
    assert client.get(path).status_code == 200


def test_logout(client):
    assert client.post("/api/auth/logout").status_code == 200  # no session is fine
    login(client)
    assert client.get("/api/scripts").status_code == 200
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    assert "max-age=0" in r.headers["set-cookie"].lower()
    assert client.get("/api/scripts").status_code == 401
    assert client.get("/api/auth/status").json()["authenticated"] is False


@pytest.mark.parametrize("bad", ["", "a.b", "!!!.x.y", "abc.notanint.ff", "a.b.c.d"])
def test_malformed_cookie_is_401_not_500(client, bad):
    assert client.get("/api/scripts", headers=cookie_header(bad)).status_code == 401


def test_crafted_tokens_via_header(client):
    expired = app_main.make_session_token(USER, issued=int(time.time()) - app_main.SESSION_MAX_AGE - 5)
    assert client.get("/api/scripts", headers=cookie_header(expired)).status_code == 401
    assert client.get("/api/scripts", headers=cookie_header(app_main.make_session_token(USER))).status_code == 200


def test_non_ascii_credentials(client, monkeypatch):
    monkeypatch.setattr(app_main, "USERS", {"zoë": "pässwörd✓"})
    assert login(client, "zoë", "wrong").status_code == 401
    assert login(client, "zoë", "pässwörd✓").status_code == 200
    assert client.get("/api/auth/status").json()["username"] == "zoë"
    assert client.get("/api/languages").status_code == 200


def test_no_users_configured(client, monkeypatch):
    monkeypatch.setattr(app_main, "USERS", {})
    assert client.get("/api/auth/status").json()["users_configured"] is False
    assert login(client).status_code == 401
