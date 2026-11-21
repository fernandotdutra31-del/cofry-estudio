import time

from fastapi.testclient import TestClient
import pytest

from cofry import media
from cofry.config import Problem
from cofry.main import app


@pytest.mark.parametrize("available", [True, False])
def test_youtube_is_exposed_only_after_startup_check(monkeypatch, tmp_path, available):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROCESSING_API_KEY", "api-test")
    monkeypatch.setenv("WEBHOOK_SECRET", "webhook-test")
    monkeypatch.setenv("ENABLE_URL_DOWNLOAD", "true")
    monkeypatch.setenv("YOUTUBE_CHECK_URL", "https://youtu.be/BaW_jenozKc")

    def fetch(url, directory, settings, _check, youtube_timeout):
        assert url == "https://www.youtube.com/watch?v=BaW_jenozKc"
        assert settings.youtube is True and settings.max_seconds == 60
        assert settings.max_bytes <= 8 * 1024**2 and youtube_timeout < 60
        if not available:
            raise Problem("Acesso recusado.")
        target = directory / "sample.mp4"
        target.write_bytes(b"sample")
        return target

    monkeypatch.setattr(media, "download", fetch)
    with TestClient(app) as client:
        assert client.post("/studio/session", headers={"X-API-Key": "api-test"}).status_code == 200
        for _ in range(100):
            mode = client.get("/studio/session").json()
            if mode["youtube_status"] != "verificando":
                break
            time.sleep(.01)
        assert mode["youtube_status"] == ("disponivel" if available else "indisponivel")
        assert client.get("/health").json()["url_download_habilitado"] is available
        assert not list(tmp_path.glob("task-youtube-check-*"))
