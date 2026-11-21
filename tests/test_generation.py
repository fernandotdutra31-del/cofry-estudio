from pathlib import Path
import shutil
import subprocess
from fastapi.testclient import TestClient
import pytest

from cofry.generation import Generation, plan, provider_payload
from cofry import generation
from cofry.main import app


@pytest.fixture
def owner(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROCESSING_API_KEY", "api")
    monkeypatch.setenv("WEBHOOK_SECRET", "hook")
    monkeypatch.setenv("STUDIO_ACCESS_KEY", "studio")
    monkeypatch.delenv("FAL_KEY", raising=False)
    with TestClient(app) as client:
        assert client.get("/studio/generations").status_code == 401
        assert client.post("/studio/session", headers={"X-API-Key": "studio"}).status_code == 200
        yield client


def test_demo_assets_and_history(owner):
    assert owner.get("/studio/generations").json()["mode"] == "demo"
    for model, duration, format in (("nano-banana-2", None, "image/jpeg"), ("minimax-h3-max", 20, "video/mp4")):
        assert owner.post("/studio/generations", json={"model_id": model, "duration": duration,
                                                     "prompt": "Café entre as nuvens"}).status_code == 202
        run = owner.get("/studio/generations").json()["runs"][0]
        assert run["status"] == "completed" and run["mode"] == "demo" and run["estimate_usd"] == 0
        response = owner.get(run["url"])
        assert response.status_code == 200 and response.headers["content-type"] == format
    assert owner.get("/studio/owner").json()["ledger"][0]["kind"] == "demo"


def test_pause_and_billable_confirmation(owner, monkeypatch):
    data = {"model_id": "veo-3-1", "duration": 8, "prompt": "Um planeta visto de longe"}
    owner.patch("/studio/owner", json={"paused": True})
    assert owner.post("/studio/generations", json=data).status_code == 409
    owner.patch("/studio/owner", json={"paused": False})
    monkeypatch.setenv("FAL_KEY", "chave-teste-sem-uso")
    assert owner.post("/studio/generations", json=data).status_code == 422
    assert owner.get("/studio/generations").json()["runs"] == []


def test_multiscene_and_estimate():
    request = Generation(model_id="kling-v3-pro", prompt="Viagem", duration=28)
    parts, estimate = plan(request)
    assert sum(parts) == 28 and len(parts) == 2 and min(parts) >= 3
    assert estimate == 3.136
    prepared = provider_payload(request.model_dump(), parts[0], 0, 2)
    assert prepared["duration"] == str(parts[0]) and "Scene 1 of 2" in prepared["prompt"]


def test_multiscene_mp4_assembly(monkeypatch, tmp_path):
    sample = Path(generation.__file__).with_name("assets") / "demo-video.mp4"
    monkeypatch.setattr(generation, "save_media", lambda _url, file, _limit: shutil.copyfile(sample, file))
    identifier = "a" * 32
    assert generation.finish_media({"id": identifier}, ["first", "second"], "video", tmp_path).endswith(identifier)
    result = tmp_path / "generations" / identifier / "resultado.mp4"
    seconds = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                       "-of", "csv=p=0", str(result)])
    assert 5.9 <= float(seconds) <= 6.1
