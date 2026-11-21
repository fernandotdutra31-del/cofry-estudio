import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from cofry import main, social
from cofry.schema import Job
from cofry.studio import CALLBACK


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROCESSING_API_KEY", "api-test-key")
    monkeypatch.setenv("WEBHOOK_SECRET", "webhook-test-key")
    monkeypatch.setenv("STUDIO_ACCESS_KEY", "studio-test-key")
    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    monkeypatch.delenv("STORAGE_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(main, "work", lambda _queue, _settings, stop: stop.wait())
    with TestClient(main.app, base_url="https://cofry.example.com") as test_client:
        yield test_client


def login(client):
    assert client.get("/studio/social").status_code == 401
    assert client.post("/studio/session", headers={"X-API-Key": "studio-test-key"}).status_code == 200


def schedule(hours=25):
    return (datetime.now(social.BRASILIA) + timedelta(hours=hours)).isoformat()


def body(**overrides):
    return {
        "project_id": "demo", "cut_id": "demo", "account_id": "demo-instagram",
        "scheduled_for": schedule(), "caption": "Veja este corte de exemplo. #cofry",
        "consent": False, "idempotency_key": "create-day-1", **overrides,
    }


def test_demo_create_edit_cancel_and_duplicate_guard(client, monkeypatch):
    monkeypatch.setattr(social, "outbound", lambda *_args, **_kwargs: pytest.fail("Demo contacted provider"))
    login(client)
    initial = client.get("/studio/social").json()
    assert initial["mode"] == "demo"
    assert {account["platform"] for account in initial["accounts"]} == {"instagram", "tiktok", "youtube"}
    assert initial["posts"] == []

    data = body()
    response = client.post("/studio/social/posts", json=data)
    assert response.status_code == 201
    post = response.json()
    path = "/studio/social/posts/" + post["id"]
    assert post["status"] == "scheduled" and post["platform"] == "instagram"
    assert client.post("/studio/social/posts", json=data).json() == post
    assert client.post("/studio/social/posts", json={**data, "caption": "Outra legenda"}).status_code == 409
    assert client.post("/studio/social/posts", json={**data, "idempotency_key": "duplicate"}).status_code == 409

    updated = client.patch(path, json={"scheduled_for": schedule(50), "caption": "Legenda nova. #cofry"})
    assert updated.status_code == 200 and updated.json()["caption"] == "Legenda nova. #cofry"
    assert client.delete(path).json()["status"] == "cancelled"
    assert client.delete(path).json()["status"] == "cancelled"
    assert client.patch(path, json={"caption": "Inalterável"}).status_code == 409
    assert client.get("/studio/social").json()["posts"][0]["status"] == "cancelled"


def test_demo_checks_future_and_express_tiktok_consent(client):
    login(client)
    assert client.post("/studio/social/posts", json=body(scheduled_for=schedule(-1))).status_code == 422
    tiktok = body(account_id="demo-tiktok")
    no_consent = client.post("/studio/social/posts", json=tiktok)
    assert no_consent.status_code == 422 and "autorize" in no_consent.json()["detail"]
    assert client.post("/studio/social/posts", json={**tiktok, "consent": "yes"}).status_code == 422
    assert client.post("/studio/social/posts", json={**tiktok, "consent": True}).status_code == 201


class ZernioFake:
    def __init__(self):
        self.calls = []
        self.posts = {}
        self.uploads = []
        self.lose_reply = False

    @contextmanager
    def request(self, url, method, body, headers, read_timeout):
        parsed = urlsplit(url)
        route = parsed.path
        if parsed.hostname == "cdn.example.com":
            assert method == "PUT" and "Authorization" not in headers
            assert headers["Content-Type"] == "video/mp4"
            self.uploads.append(body.read())
            yield Response(200, None)
            return
        assert parsed.hostname == "zernio.com"
        assert headers["Authorization"] == "Bearer zernio-test-key"
        data = json.loads(body) if body else None
        self.calls.append((method, route, data, headers))
        if route == "/api/v1/accounts":
            response = Response(200, {"accounts": [
                {"_id": "connected-tiktok", "platform": "tiktok", "username": "cofry", "isActive": True},
                {"_id": "offline-youtube", "platform": "youtube", "displayName": "Desconectado", "isActive": False},
                {"_id": "unsupported", "platform": "facebook", "username": "outra rede"},
            ]})
        elif route == "/api/v1/media/presign":
            assert "size" in data and data["contentType"] == "video/mp4"
            response = Response(200, {
                "uploadUrl": "https://cdn.example.com/upload?signature=test",
                "publicUrl": "https://cdn.example.com/public.mp4",
            })
        elif route == "/api/v1/posts" and method == "GET":
            external = parse_qs(parsed.query)["externalId"][0]
            response = Response(200, {"posts": [
                item for item in self.posts.values() if item["externalId"] == external
            ]})
        elif route == "/api/v1/posts" and method == "POST":
            assert headers["Idempotency-Key"] == data["externalId"]
            post_id = "provider-" + data["externalId"]
            self.posts[post_id] = {**data, "_id": post_id, "status": "scheduled"}
            if self.lose_reply:
                raise OSError("Interrupção simulada após agendar")
            response = Response(201, {"post": self.posts[post_id]})
        elif route.startswith("/api/v1/posts/"):
            post_id = route.rsplit("/", 1)[-1]
            if method == "DELETE":
                self.posts.pop(post_id, None)
                response = Response(200, {"success": True})
            else:
                current = self.posts[post_id]
                if method == "PUT":
                    current.update(data)
                response = Response(200, {"post": current})
        else:
            pytest.fail("Unexpected request")
        yield response


class Response:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    def read(self, *_args, **_kwargs):
        return json.dumps(self.payload).encode() if self.payload is not None else b""


def processed_project():
    state = main.app.state
    job = Job(job_id="finished-project", video_url="https://example.com/original.mp4", callback_url=CALLBACK)
    state.queue.submit(job)
    state.queue.claim()
    key = "studio/finished-project/clip-1/revision/clip.mp4"
    file = state.settings.data / "studio-library" / key
    file.parent.mkdir(parents=True)
    file.write_bytes(b"test clip")
    state.queue.finish(job, None, cuts=[{
        "id": "clip-1", "titulo": "Corte processado", "url_mp4": "/studio/media/" + key,
    }], duration=305)


def test_real_mode_presigns_local_clip_edits_and_cancels(client, monkeypatch):
    login(client)
    demo = client.post("/studio/social/posts", json=body()).json()
    monkeypatch.setenv("ZERNIO_API_KEY", "zernio-test-key")
    fake = ZernioFake()
    monkeypatch.setattr(social, "outbound", fake.request)
    processed_project()

    overview = client.get("/studio/social").json()
    assert overview["mode"] == "real" and overview["posts"] == []
    assert {account["id"]: account["active"] for account in overview["accounts"]} == {
        "connected-tiktok": True, "offline-youtube": False,
    }
    real_data = body(project_id="finished-project", cut_id="clip-1", account_id="connected-tiktok", consent=True)
    response = client.post("/studio/social/posts", json=real_data)
    assert response.status_code == 201, response.text
    post = response.json()
    assert post["status"] == "scheduled" and fake.uploads == [b"test clip"]
    upstream = fake.posts["provider-" + post["id"]]
    assert upstream["mediaItems"] == [{"type": "video", "url": "https://cdn.example.com/public.mp4"}]
    settings = upstream["platforms"][0]["platformSpecificData"]["tiktokSettings"]
    assert settings["privacyLevel"] == "PUBLIC_TO_EVERYONE" and settings["expressConsentGiven"] is True
    assert upstream["timezone"] == "America/Sao_Paulo"
    assert client.post("/studio/social/posts", json=real_data).json() == post and len(fake.posts) == 1

    path = "/studio/social/posts/" + post["id"]
    changed = client.patch(path, json={"caption": "Corte editado. #cofry"})
    assert changed.status_code == 200
    assert upstream["platforms"][0]["customContent"] == "Corte editado. #cofry"
    assert client.delete(path).json()["status"] == "cancelled" and not fake.posts

    monkeypatch.delenv("ZERNIO_API_KEY")
    assert client.get("/studio/social").json()["posts"] == [demo]


def test_real_retry_reconciles_lost_provider_response(client, monkeypatch):
    login(client)
    monkeypatch.setenv("ZERNIO_API_KEY", "zernio-test-key")
    fake = ZernioFake()
    fake.lose_reply = True
    monkeypatch.setattr(social, "outbound", fake.request)
    processed_project()
    data = body(project_id="finished-project", cut_id="clip-1", account_id="connected-tiktok", consent=True)
    first = client.post("/studio/social/posts", json=data)
    assert first.status_code == 502 and len(fake.posts) == 1
    retry = client.post("/studio/social/posts", json=data)
    assert retry.status_code == 201 and retry.json()["status"] == "scheduled"
    assert len(fake.posts) == 1 and len(fake.uploads) == 1
    assert len([call for call in fake.calls if call[:2] == ("POST", "/api/v1/posts")]) == 1
