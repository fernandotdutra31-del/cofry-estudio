from fastapi.testclient import TestClient

from cofry.main import app
from cofry.owner import endpoint_allowed


def test_owner_notification_and_pause(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROCESSING_API_KEY", "api")
    monkeypatch.setenv("WEBHOOK_SECRET", "webhook")
    monkeypatch.setenv("STUDIO_ACCESS_KEY", "owner")
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    with TestClient(app) as client:
        assert client.get("/studio/owner").status_code == 401
        assert client.post("/studio/session", headers={"X-API-Key": "owner"}).status_code == 200
        assert client.get("/studio/owner").json()["push"] == {"available": False, "public_key": None}
        assert client.post("/studio/owner/notifications", json={
            "title": "Aviso de teste", "body": "Seu novo corte está pronto.",
        }).status_code == 201
        view = client.get("/studio/owner").json()
        assert view["unread"] == 1 and view["notifications"][0]["kind"] == "manual"
        assert client.post("/studio/owner/notifications/read").status_code == 200
        assert client.get("/studio/owner").json()["unread"] == 0
        assert client.patch("/studio/owner", json={"paused": True}).json()["paused"] is True
        assert client.post("/studio/projects", json={"url": "https://example.com/video.mp4"}).status_code == 409
        assert client.patch("/studio/owner", json={"paused": False}).json()["paused"] is False
        assert client.post("/studio/projects", json={"url": "http://example.com/video.mp4"}).status_code == 422


def test_push_destinations():
    assert endpoint_allowed("https://fcm.googleapis.com/fcm/send/example")
    assert endpoint_allowed("https://updates.push.services.mozilla.com/wpush/v2/example")
    assert not endpoint_allowed("https://fcm.googleapis.com.other.example/send")
    assert not endpoint_allowed("https://127.0.0.1/push")
    assert not endpoint_allowed("http://fcm.googleapis.com/fcm/send/example")
