import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from cofry import media, selection, service
from cofry.main import app
from cofry.job_store import Queue
from cofry.schema import Job

HEADERS = {"X-API-Key": "teste"}
JOB = {
    "job_id": "video-exemplo", "video_url": "https://source.example/video.mp4",
    "callback_url": "https://callback.example/job",
    "options": {"quantidade": 2, "duracao_min": 3, "duracao_pref": 5, "duracao_max": 12,
                "seguir_rosto": True, "estilo_legenda": "karaoke", "idioma": "auto"},
}


@pytest.fixture(scope="session")
def fixture_video(tmp_path_factory):
    folder = tmp_path_factory.mktemp("fixture")
    target = folder / "video.mp4"
    duration = int(os.getenv("TEST_VIDEO_SECONDS", "126"))
    subprocess.run(["ffmpeg", "-v", "error", "-filter_threads", "1", "-f", "lavfi", "-i",
                    f"testsrc2=size=320x180:rate=30:duration={duration}", "-f", "lavfi", "-i",
                    "flite=text='Good stories start with a clear question. Every useful answer needs evidence.':voice=slt",
                    "-af", "apad=pad_dur=1", "-c:a", "pcm_s16le", str(folder / "speech.wav"), "-map", "0:v:0",
                    "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast", str(folder / "visual.mp4")],
                   check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(folder / "visual.mp4"), "-stream_loop", "-1",
                    "-i", str(folder / "speech.wav"), "-t", str(duration), "-map", "0:v:0", "-map",
                    "1:a:0", "-af", "volume=0:enable='between(t,45,190)+gte(t,215)'",
                    "-c:v", "copy", "-c:a", "aac", str(target)], check=True)
    return target


def make_words():
    return [{"inicio": start + i * .39, "fim": start + i * .39 + .27, "texto": text}
            for start, line in [(1, "Uma pergunta clara muda toda a história."),
                                (5, "A resposta fica mais forte com evidências."),
                                (11, "Um exemplo concreto transforma a sua explicação."),
                                (15, "Mostre os resultados antes de explicar o método.")]
            for i, text in enumerate(line.split())]


def as_result(first, last):
    return dict(primeira=first, ultima=last, titulo="Conte histórias claras",
                gancho="Por que sua história perde força?", nota=94, motivo="Exemplo direto",
                legendas={"instagram": "Salve a ideia #história", "tiktok": "Confira o exemplo #ideia",
                          "youtube": "Veja o método #Shorts"})


def wait_for(action, timeout=120):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if outcome := action():
            return outcome
        time.sleep(.15)
    pytest.fail("O processamento não chegou ao estado esperado.")


def prepare(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROCESSING_API_KEY", "teste")
    monkeypatch.setenv("WEBHOOK_SECRET", "segredo")


def test_pipeline(monkeypatch, tmp_path, fixture_video):
    prepare(monkeypatch, tmp_path)
    saved = tmp_path / "published"
    saved.mkdir()
    events = []
    llm = []
    attempts = []
    original_run = media.run

    def download(url, folder, settings, check):
        path = folder / "source.media"
        shutil.copyfile(fixture_video, path)
        return path

    def run(args, folder, check, message, timeout=1800):
        if args[1] != str(service.TRANSCRIBER):
            return original_run(args, folder, check, message, timeout)
        model = os.getenv("TEST_WHISPER_MODEL")
        if model:
            original_run([*args[:2], model, *args[3:]], folder, check, message, timeout)
            words = json.loads((folder / "words.json").read_text())
            assert len(words) > 10
            if int(os.getenv("TEST_VIDEO_SECONDS", "126")) > 190:
                assert any(word["inicio"] > 180 for word in words)
            return
        (folder / "words.json").write_text(json.dumps(make_words()))

    def ask(settings, data, correction):
        llm.append(correction)
        if len(llm) == 1:
            return "JSON inválido"
        rows = data["frases"]
        if not os.getenv("TEST_WHISPER_MODEL"):
            return json.dumps({"cortes": [as_result(0, 1), as_result(2, 3), as_result(1, 2)]})
        options = [(a["id"], b["id"]) for i, a in enumerate(rows[:40])
                   for b in rows[i+1:i+5] if 5 <= b["fim"] - a["inicio"] <= 10]
        return json.dumps({"cortes": [as_result(*pair) for pair in options[:20]]})


    class Storage:
        def put(self, job, clip, video, cover, check, written):
            urls = {}
            for field, path in (("url_mp4", video), ("url_capa", cover)):
                key = f"{clip}-{len(list(saved.iterdir()))}-{path.name}"
                written(key)
                shutil.copyfile(path, saved / key)
                urls[field] = f"https://cdn.example/{key}"
            return urls

        def delete(self, keys):
            for key in keys:
                (saved / key).unlink(missing_ok=True)

    @contextmanager
    def webhook(url, method, body, headers, **kwargs):
        assert method == "POST"
        signature = hmac.new(b"segredo", body, hashlib.sha256).hexdigest()
        assert headers["X-Signature"] == signature
        assert headers["X-Job-Id"] == JOB["job_id"]
        attempts.append(time.monotonic())
        status = 503 if len(attempts) < 3 else 204
        if status == 204:
            events.append(json.loads(body))
        yield type("Response", (), {"status": status})()

    monkeypatch.setattr(media, "download", download)
    monkeypatch.setattr(media, "run", run)
    monkeypatch.setattr(selection, "ask", ask)
    monkeypatch.setattr(service, "Storage", Storage)
    monkeypatch.setattr(service, "request", webhook)
    path = "/jobs/video-exemplo"
    with TestClient(app) as client:
        assert client.get("/health").json() == {"ok": True, "url_download_habilitado": False}
        assert client.post("/jobs", json=JOB).status_code == 401
        bad_job = dict(JOB, callback_url="http://callback.example/job")
        assert client.post("/jobs", json=bad_job, headers=HEADERS).status_code == 422
        assert client.post("/jobs", json=JOB, headers=HEADERS).status_code == 202
        assert client.post("/jobs", json=JOB, headers=HEADERS).status_code == 202
        assert client.post("/jobs", json=dict(JOB, video_url="https://x.example/2"), headers=HEADERS).status_code == 409

        def terminal():
            result = client.get(path, headers=HEADERS).json()
            return result if result["status"] in {"completed", "failed"} else None

        result = wait_for(terminal, timeout=600)
        assert result["status"] == "completed", result
        assert result["progresso"] == 100
        assert len(result["cortes"]) == 2
        first, second = result["cortes"]
        assert first["fim"] <= second["inicio"]
        assert llm[1]
        wait_for(lambda: events)
        assert events[0] == result
        assert len(attempts) == 3
        assert attempts[1] - attempts[0] >= 2
        assert attempts[2] - attempts[1] >= 4
        output = saved / first["url_mp4"].split("/")[-1]
        streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams",
                                                       "-of", "json", str(output)]))["streams"]
        video, audio = streams
        assert (video["codec_name"], video["width"], video["height"], video["r_frame_rate"]) == ("h264", 1080, 1920, "30/1")
        assert audio["codec_name"] == "aac"
        assert float(video["duration"]) < first["duracao"] - .4
        assert abs(float(video["duration"]) - float(audio["duration"])) < .15

        edit_path = path + f"/cortes/{first['id']}/rerender"
        edit = {"inicio": 1, "fim": 7, "legenda": {"blocos": [
            {"inicio": 1.1, "fim": 2, "texto": "Uma edição aprovada."}], "estilo": "laranja"},
            "gancho": "Uma pergunta muda tudo", "manchete": True}
        assert client.post(edit_path, json=edit, headers=HEADERS).status_code == 202
        revised = wait_for(terminal)
        assert revised["status"] == "completed", revised
        assert revised["cortes"][0]["url_mp4"] != first["url_mp4"]
        assert revised["cortes"][1] == second
        wait_for(lambda: len(events) == 2)
        assert events[1] == revised
        assert client.delete(path, headers=HEADERS).status_code == 202
        wait_for(lambda: not list(saved.iterdir()))
        assert client.get(path, headers=HEADERS).json()["status"] == "cancelled"
        wait_for(lambda: not list((tmp_path / "data").glob("task-*")))


def test_failure_callback(monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path)
    events = []

    def denied(*args):
        from cofry.config import Problem
        raise Problem("Não foi possível baixar este link. Envie o arquivo.")

    @contextmanager
    def webhook(url, method, body, headers, **kwargs):
        events.append(json.loads(body))
        yield type("Response", (), {"status": 200})()

    monkeypatch.setattr(media, "download", denied)
    monkeypatch.setattr(service, "request", webhook)
    with TestClient(app) as client:
        assert client.post("/jobs", json=JOB, headers=HEADERS).status_code == 202
        wait_for(lambda: events)
        result = client.get("/jobs/video-exemplo", headers=HEADERS).json()
        assert events[0] == result
        assert result["status"] == "failed"
        assert result["erro"] == "Não foi possível baixar este link. Envie o arquivo."
        assert not list((tmp_path / "data").glob("task-*"))


def test_recovers_claimed_job(tmp_path):
    queue = Queue(tmp_path)
    queue.submit(Job.model_validate(JOB))
    row = queue.claim()
    queue.progress(row["id"], "renderizando", 70)
    restarted = Queue(tmp_path)
    restarted.recover()
    assert json.loads(restarted.get(row["id"])["result"])["progresso"] == 0
    assert restarted.claim()["id"] == row["id"]


def test_untrusted_urls(monkeypatch):
    from cofry.config import Problem
    from cofry.network import public_ip, validate
    for link in ("http://example.org", "https://name@example.org", "https://example.org:444"):
        with pytest.raises(Problem):
            validate(link)
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("8.8.8.8", 443)),
        (None, None, None, None, ("127.0.0.1", 443)),
    ])
    with pytest.raises(Problem):
        public_ip("example.org")


def test_cancels_active_job(monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path)

    def downloading(url, folder, settings, check):
        (folder / "source.media").write_bytes(b"download parcial")
        while True:
            check()
            time.sleep(.05)

    monkeypatch.setattr(media, "download", downloading)
    with TestClient(app) as client:
        assert client.post("/jobs", json=JOB, headers=HEADERS).status_code == 202
        path = "/jobs/video-exemplo"
        wait_for(lambda: list((tmp_path / "data").glob("task-*/source.media")))
        assert client.delete(path, headers=HEADERS).status_code == 202
        wait_for(lambda: not list((tmp_path / "data").glob("task-*")))
        result = client.get(path, headers=HEADERS).json()
        assert result["status"] == "cancelled"
        assert result["cortes"] == []


def test_studio_upload_and_local_rerender(monkeypatch, tmp_path, fixture_video):
    prepare(monkeypatch, tmp_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("STUDIO_USE_S3", "false")
    original_run = media.run

    def run(args, folder, check, message, timeout=1800):
        if args[1] == str(service.TRANSCRIBER):
            (folder / "words.json").write_text(json.dumps(make_words()))
            return b""
        return original_run(args, folder, check, message, timeout)

    monkeypatch.setattr(media, "run", run)
    with TestClient(app) as client:
        assert client.get("/studio").status_code == 200
        assert client.get("/studio/projects").status_code == 401
        assert client.post("/studio/session", headers=HEADERS).status_code == 200
        assert client.get("/studio/session").json()["modo"] == "local"
        posted = client.post("/studio/projects", content=fixture_video.read_bytes(), headers={
            "Content-Type": "application/octet-stream",
            "X-Studio-Options": json.dumps({"quantidade": 1, "duracao_min": 3, "duracao_pref": 6,
                                            "duracao_max": 12, "formato": "1:1"}),
        })
        assert posted.status_code == 202
        project_path = "/studio/projects/" + posted.json()["job_id"]

        def completed():
            result = client.get(project_path).json()
            return result if result["status"] in {"completed", "failed"} else None

        result = wait_for(completed)
        assert result["status"] == "completed", result
        cut = result["cortes"][0]
        assert "studio/media" in cut["url_mp4"]
        assert client.get(cut["url_mp4"], headers={"Range":"bytes=0-15"}).status_code == 206
        edit = client.post(project_path + "/cuts/" + cut["id"], json={
            "inicio": 1, "fim": 7, "gancho": "Veja a pergunta", "estilo_legenda": "laranja",
        })
        assert edit.status_code == 202
        revised = wait_for(completed)
        assert revised["status"] == "completed", revised
        assert revised["cortes"][0]["gancho"] == "Veja a pergunta"
        assert revised["cortes"][0]["url_mp4"] != cut["url_mp4"]
        assert client.delete(project_path).status_code == 202
        wait_for(lambda: client.get(revised["cortes"][0]["url_mp4"]).status_code == 404)
        assert client.delete("/studio/session").status_code == 200
        assert client.get("/studio/projects").status_code == 401


def test_studio_secure_cookie_when_proxy_terminates_tls(monkeypatch, tmp_path):
    prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("STUDIO_COOKIE_SECURE", "true")
    monkeypatch.setenv("STUDIO_ACCESS_KEY", "chave-do-estudio")
    with TestClient(app) as client:
        assert client.post("/studio/session", headers=HEADERS).status_code == 401
        response = client.post("/studio/session", headers={"X-API-Key": "chave-do-estudio"})
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]
        assert client.get("/studio/projects").status_code == 401
        assert client.get("https://testserver/studio/projects").status_code == 200
