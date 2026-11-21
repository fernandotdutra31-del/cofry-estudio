import base64
import hashlib
import json
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from cofry import google_auth
from cofry.main import app


ORIGIN = "https://cofry.example.com"
LOGIN = "/studio/auth/google"
CALLBACK = google_auth.CALLBACK_PATH
OWNER = "dono@example.com"


@pytest.fixture
def client(monkeypatch, tmp_path):
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "OWNER_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROCESSING_API_KEY", "key-api")
    monkeypatch.setenv("WEBHOOK_SECRET", "key-webhook")
    monkeypatch.setenv("STUDIO_ACCESS_KEY", "key-studio")
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as test_client:
        yield test_client


def configure(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "server-secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", ORIGIN + CALLBACK)
    monkeypatch.setenv("OWNER_EMAIL", OWNER)


def start(client):
    started = client.get(LOGIN)
    assert started.status_code == 303
    cookie = started.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("secure", "httponly", "samesite=lax"))
    auth_url = urlsplit(started.headers["location"])
    assert auth_url.hostname == "accounts.google.com"
    params = parse_qs(auth_url.query)
    assert params["scope"] == ["openid email"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == [ORIGIN + CALLBACK]
    assert "client_secret" not in params and OWNER not in auth_url.query
    return params["state"][0], params["code_challenge"][0]


def provider(monkeypatch, profile, calls):
    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def read(self, *_args, **_kwargs):
            return json.dumps(self.payload).encode()

    @contextmanager
    def request(url, *, method, body, headers, read_timeout):
        calls.append((url, method, body, headers))
        if url == google_auth.TOKEN_URL:
            yield Response({"access_token": "oauth-token", "token_type": "Bearer"})
        else:
            assert url == google_auth.USERINFO_URL
            assert headers["Authorization"] == "Bearer oauth-token"
            yield Response(profile)

    monkeypatch.setattr(google_auth, "request", request)


def test_key_login_when_google_is_unconfigured(client, monkeypatch):
    assert client.get("/studio/auth/options").json() == {"google": False, "key": True}
    assert client.get(LOGIN).headers["location"] == "/studio?auth=google_unavailable"
    assert client.get("/studio/session").status_code == 401
    assert client.post("/studio/session", headers={"X-API-Key": "key-api"}).status_code == 401
    assert client.post("/studio/session", headers={"X-API-Key": "key-studio"}).json() == {"ok": True}
    session = client.get("/studio/session").json()
    assert session["strategy"] == "key"
    assert session["user"] == {"name": "Proprietário", "email": None, "owner": True}
    assert session["credits"] == {"unlimited": True}
    monkeypatch.setenv("OWNER_EMAIL", OWNER)
    assert client.get("/studio/session").json()["user"]["email"] == OWNER
    assert client.get("/jobs/missing", headers={"X-API-Key": "key-studio"}).status_code == 401
    assert client.delete("/studio/session").status_code == 200
    assert client.get("/studio/session").status_code == 401


def test_google_owner_uses_pkce_and_creates_session(client, monkeypatch):
    configure(monkeypatch)
    calls = []
    provider(monkeypatch, {"email": OWNER.upper(), "email_verified": True, "sub": "owner-id"}, calls)
    assert client.get("/studio/auth/options").json() == {"google": True, "key": True}
    state, challenge = start(client)
    result = client.get(CALLBACK, params={"code": "authorization-code", "state": state})
    assert result.status_code == 303
    assert result.headers["location"] == "/studio"
    assert result.headers["referrer-policy"] == "no-referrer"
    session_cookie = next(
        cookie.lower() for cookie in result.headers.get_list("set-cookie") if cookie.startswith("cofry_session=")
    )
    assert all(flag in session_cookie for flag in ("secure", "httponly", "samesite=strict", "max-age=86400"))
    assert [(url, verb) for url, verb, _, _ in calls] == [
        (google_auth.TOKEN_URL, "POST"), (google_auth.USERINFO_URL, "GET"),
    ]
    token_request = parse_qs(calls[0][2].decode())
    assert token_request["client_secret"] == ["server-secret"]
    assert token_request["code"] == ["authorization-code"]
    verifier = token_request["code_verifier"][0]
    assert base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode() == challenge
    assert client.get("/studio/session").json()["strategy"] == "google"
    assert client.get("/studio/projects").status_code == 200
    assert client.get("/jobs/missing").status_code == 401

    # A consumed callback cannot be repeated, and an owner change revokes an existing session.
    replay = client.get(CALLBACK, params={"code": "authorization-code", "state": state})
    assert replay.headers["location"] == "/studio?auth=google_invalid" and len(calls) == 2
    monkeypatch.setenv("OWNER_EMAIL", "novo@example.com")
    assert client.get("/studio/session").status_code == 401


@pytest.mark.parametrize(
    "profile",
    [
        {"email": "visitante@example.com", "email_verified": True, "sub": "visitor-id"},
        {"email": OWNER, "email_verified": False, "sub": "owner-id"},
        {"email": OWNER, "email_verified": "true", "sub": "owner-id"},
        {"email": OWNER, "email_verified": True},
    ],
)
def test_google_denies_non_owners_and_unverified_accounts(client, monkeypatch, profile):
    configure(monkeypatch)
    provider(monkeypatch, profile, [])
    state, _ = start(client)
    result = client.get(CALLBACK, params={"code": "authorization-code", "state": state})
    assert result.headers["location"] == "/studio?auth=google_denied"
    assert client.get("/studio/session").status_code == 401


def test_google_rejects_bad_state_before_contacting_provider(client, monkeypatch):
    configure(monkeypatch)
    calls = []
    provider(monkeypatch, {}, calls)
    state, _ = start(client)
    incorrect = ("A" if not state.startswith("A") else "B") + state[1:]
    result = client.get(CALLBACK, params={"code": "authorization-code", "state": incorrect})
    assert result.headers["location"] == "/studio?auth=google_invalid"
    assert not calls


def test_google_returns_a_retry_status_for_provider_failure(client, monkeypatch):
    configure(monkeypatch)

    def unavailable(*_args, **_kwargs):
        raise OSError("provider unavailable")

    monkeypatch.setattr(google_auth, "request", unavailable)
    state, _ = start(client)
    result = client.get(CALLBACK, params={"code": "authorization-code", "state": state})
    assert result.headers["location"] == "/studio?auth=google_retry"
    assert client.get("/studio/session").status_code == 401
