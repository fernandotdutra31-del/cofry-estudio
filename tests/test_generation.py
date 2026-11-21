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


def test_cloudflare_provider_saves_prompt_image_and_animation(monkeypatch, tmp_path):
    import time

    from cofry import cloudflare_media
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROCESSING_API_KEY", "api")
    monkeypatch.setenv("WEBHOOK_SECRET", "hook")
    monkeypatch.setenv("STUDIO_ACCESS_KEY", "studio")
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_AI_TOKEN", "token-de-teste-nunca-usado")
    monkeypatch.setenv("CLOUDFLARE_WORKERS_FREE", "true")
    example = Path(generation.__file__).with_name("assets") / "demo-image.jpg"
    prompts = []

    def provider(_credentials, prompt):
        prompts.append(prompt)
        return example.read_bytes()

    monkeypatch.setattr(cloudflare_media, "generate_image", provider)
    with TestClient(app) as client:
        assert client.post("/studio/session", headers={"X-API-Key": "studio"}).status_code == 200
        catalog = client.get("/studio/generations").json()
        assert catalog["mode"] == "real"
        assert {entry["id"] for entry in catalog["models"] if entry["available"]} == {
            "flux-cloudflare", "animacao-cloudflare",
        }
        assert client.post("/studio/generations", json={
            "model_id": "veo-3-1", "prompt": "Cena de teste", "duration": 8,
        }).status_code == 409
        for model_id, prompt, extension, duration in (
            ("flux-cloudflare", "Crie uma foto vertical de um astronauta tomando café em São Paulo", "jpg", None),
            ("animacao-cloudflare", "Gere um vídeo vertical de 3 segundos de um castelo", "mp4", 3),
        ):
            response = client.post("/studio/generations", json={
                "model_id": model_id, "prompt": prompt, "ratio": "9:16", "duration": duration,
            })
            assert response.status_code == 202
            identifier = response.json()["id"]
            for _ in range(150):
                row = next(row for row in client.get("/studio/generations").json()["runs"] if row["id"] == identifier)
                if row["status"] in {"failed", "completed"}:
                    break
                time.sleep(.1)
            assert row["status"] == "completed", row
            assert row["estimate_usd"] == 0 and prompts[-1] == prompt
            result = client.get(row["url"])
            assert result.status_code == 200
            path = tmp_path / f"probe.{extension}"
            path.write_bytes(result.content)
            details = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                               "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)])
            assert details.strip() == b"540,960"
        assert len(prompts) == 2
