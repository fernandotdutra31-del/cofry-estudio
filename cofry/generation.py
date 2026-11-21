import json
import logging
import math
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field
from urllib3.exceptions import HTTPError

from . import media as media_tools
from . import cloudflare_media
from .config import Problem
from .network import read_limited, request as outbound, validate
from .schema import Model
from .studio import guard, require_generation_open

router = APIRouter(prefix="/studio/generations")
LOG = logging.getLogger(__name__)
QUEUE_URL = "https://queue.fal.run"
MODELS = {
    "nano-banana-2": ("Nano Banana 2", "imagem", "fal-ai/nano-banana-2", .08, ()),
    "flux-schnell": ("FLUX Schnell", "imagem", "fal-ai/flux/schnell", .003, ()),
    "minimax-h3-max": ("MiniMax H3 Max", "video", "minimax/h3-max/text-to-video", .05, tuple(range(3, 16))),
    "kling-v3-pro": ("Kling v3 Pro", "video", "fal-ai/kling-video/v3/pro/text-to-video", .112, tuple(range(3, 16))),
    "veo-3-1": ("Veo 3.1", "video", "fal-ai/veo3.1", .20, (4, 6, 8)),
    "flux-cloudflare": ("FLUX · Workers AI", "imagem", "", 0, ()),
    "animacao-cloudflare": ("Animação de imagem · Workers AI", "video", "", 0, tuple(range(3, 31))),
}
RATIOS = {"16:9": (960, 544), "9:16": (544, 960), "1:1": (768, 768)}


class Generation(Model):
    model_id: str = Field(max_length=40)
    prompt: str = Field(min_length=1, max_length=2000)
    ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    duration: int | None = Field(None, ge=3, le=30)
    confirmed: bool = False


def durations(seconds, allowed):
    options = {0: []}
    for total in range(1, seconds + 1):
        paths = [options[total-n] + [n] for n in reversed(allowed) if total-n in options]
        if paths:
            options[total] = min(paths, key=len)
    return options.get(seconds)


def catalog():
    cloudflare_key = cloudflare_media.Credentials.configured()
    fal = bool(os.getenv("FAL_KEY"))
    return [{
        "id": name, "label": item[0], "kind": item[1], "usd": item[3],
        "provider": "cloudflare" if name in cloudflare_media.MODEL_IDS else "fal",
        "free": name in cloudflare_media.MODEL_IDS,
        "available": bool(cloudflare_key) if name in cloudflare_media.MODEL_IDS else (fal or not cloudflare_key),
        "durations": [n for n in range(3, 31) if durations(n, item[4])] if item[4] else [],
        "ratios": list(RATIOS) if name != "veo-3-1" else ["16:9", "9:16"],
    } for name, item in MODELS.items() if cloudflare_key or name not in cloudflare_media.MODEL_IDS]


def plan(payload):
    if payload.model_id not in MODELS:
        raise HTTPException(422, "Selecione um modelo do catálogo.")
    _, kind, _, rate, allowed = MODELS[payload.model_id]
    if payload.model_id == "veo-3-1" and payload.ratio == "1:1":
        raise HTTPException(422, "Veo 3.1 aceita 16:9 ou 9:16.")
    if kind == "imagem":
        if payload.duration is not None:
            raise HTTPException(422, "Imagens não usam duração.")
        width, height = RATIOS[payload.ratio]
        return [0], rate * (math.ceil(width * height / 1e6) if payload.model_id == "flux-schnell" else 1)
    parts = durations(payload.duration or 0, allowed)
    if not parts:
        raise HTTPException(422, "Selecione uma duração aceita pelo modelo.")
    return parts, round(rate * sum(parts), 3)


def schema(queue):
    with queue.connect(True) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS studio_generations (
                id TEXT PRIMARY KEY, mode TEXT NOT NULL, status TEXT NOT NULL, request TEXT NOT NULL,
                estimate REAL NOT NULL, parts TEXT NOT NULL, provider TEXT,
                output TEXT, error TEXT, created REAL NOT NULL
            )
        """)


def format_row(row):
    data = json.loads(row["request"])
    return {
        "id": row["id"], "mode": row["mode"], "status": row["status"],
        "kind": MODELS[data["model_id"]][1], "model": MODELS[data["model_id"]][0],
        "prompt": data["prompt"], "estimate_usd": row["estimate"], "url": row["output"],
        "error": row["error"], "created_at": datetime.fromtimestamp(row["created"], UTC).isoformat(),
        "model_id": data["model_id"],
    }


@router.get("")
def list_generations(request: Request):
    state = guard(request)
    schema(state.queue)
    with state.queue.connect() as db:
        rows = db.execute("SELECT * FROM studio_generations ORDER BY created DESC LIMIT 80").fetchall()
    return {"mode": "real" if os.getenv("FAL_KEY") or cloudflare_media.Credentials.configured() else "demo",
            "models": catalog(),
            "runs": [format_row(row) for row in rows]}


@router.post("", status_code=202)
def create(payload: Generation, request: Request):
    state = guard(request)
    require_generation_open(state)
    parts, estimate = plan(payload)
    is_free = payload.model_id in cloudflare_media.MODEL_IDS
    cloudflare_key = cloudflare_media.Credentials.configured()
    fal = bool(os.getenv("FAL_KEY"))
    if (is_free and not cloudflare_key) or (not is_free and cloudflare_key and not fal):
        raise HTTPException(409, "Este modelo ainda não está conectado.")
    demo = not (is_free or fal)
    if not demo and not is_free and not payload.confirmed:
        raise HTTPException(422, "Confirme o custo estimado antes de gerar.")
    schema(state.queue)
    job = uuid4().hex
    _, kind, *_ = MODELS[payload.model_id]
    output = "/studio/assets/demo-image.jpg" if kind == "imagem" else "/studio/assets/demo-video.mp4"
    with state.queue.connect(True) as db:
        if not demo and db.execute(
            "SELECT id FROM studio_generations WHERE status IN ('queued','submitting','running')"
        ).fetchone():
            raise HTTPException(409, "Já há uma geração em andamento.")
        db.execute("INSERT INTO studio_generations VALUES (?,?,?,?,?,?,NULL,?,NULL,?)", (
            job, "demo" if demo else "real", "completed" if demo else "queued", payload.model_dump_json(),
            0 if demo else estimate, json.dumps(parts), output if demo else None, time.time(),
        ))
        if demo:
            db.execute("INSERT INTO studio_ledger(kind,detail,created) VALUES (?,?,?)",
                       ("demo", f"Demonstração · {MODELS[payload.model_id][0]}", time.time()))
    return {"id": job}


@router.get("/media/{identifier}")
def media(identifier: str, request: Request, download: bool = False):
    state = guard(request)
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise HTTPException(404)
    schema(state.queue)
    with state.queue.connect() as db:
        row = db.execute("SELECT request,status,mode FROM studio_generations WHERE id=?", (identifier,)).fetchone()
    if not row or row["status"] != "completed" or row["mode"] != "real":
        raise HTTPException(404, "Mídia indisponível.")
    kind = MODELS[json.loads(row["request"])["model_id"]][1]
    extension = "jpg" if kind == "imagem" else "mp4"
    file = state.settings.data / "generations" / identifier / ("resultado." + extension)
    if not file.is_file():
        raise HTTPException(404, "O arquivo expirou.")
    return FileResponse(file, filename=file.name if download else None)


def provider_call(url, key, payload=None):
    if validate(url).hostname != "queue.fal.run":
        raise Problem("A fal.ai retornou um endereço de consulta inválido.")
    try:
        with outbound(url, method="POST" if payload is not None else "GET",
                      body=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
                      read_timeout=25) as response:
            if response.status in {401, 403}:
                raise Problem("Confira a chave e o saldo na fal.ai.")
            if response.status >= 400:
                raise Problem(f"A fal.ai recusou a operação (HTTP {response.status}).")
            value = json.loads(read_limited(response, 1_000_000))
            if not isinstance(value, dict):
                raise ValueError
            return value
    except (HTTPError, OSError, ValueError):
        raise Problem("A fal.ai não respondeu. Verifique o histórico no provedor antes de tentar novamente.") from None


def provider_payload(data, length, index, total):
    name = data["model_id"]
    prompt = data["prompt"]
    if total > 1:
        prompt = f"Scene {index + 1} of {total}, consistent visual style and characters. {prompt}"
    ratio = data["ratio"]
    if name == "nano-banana-2":
        return {"prompt": prompt, "aspect_ratio": ratio, "resolution": "1K", "num_images": 1}
    if name == "flux-schnell":
        width, height = RATIOS[ratio]
        return {"prompt": prompt, "image_size": {"width": width, "height": height}, "num_images": 1}
    if name == "veo-3-1":
        return {"prompt": prompt, "aspect_ratio": ratio, "duration": f"{length}s",
                "generate_audio": False, "resolution": "720p"}
    if name == "kling-v3-pro":
        return {"prompt": prompt, "aspect_ratio": ratio, "duration": str(length), "generate_audio": False}
    return {"prompt": prompt, "aspect_ratio": ratio, "duration": length,
            "resolution": "480P", "prompt_expansion_mode": "disabled"}


def save_media(url, destination, limit):
    validate(url)
    with outbound(url, redirects=2, read_timeout=30) as reply:
        if reply.status != 200:
            raise Problem("Não foi possível baixar o resultado.")
        size = 0
        with destination.open("wb") as output:
            for block in reply.stream(256 * 1024):
                size += len(block)
                if size > limit:
                    raise Problem("O resultado excedeu o limite do armazenamento.")
                output.write(block)


def finish_media(row, urls, kind, data_dir):
    destination = data_dir / "generations" / row["id"]
    destination.mkdir(parents=True, exist_ok=True)
    ext = "jpg" if kind == "imagem" else "mp4"
    target = destination / ("resultado." + ext)
    with TemporaryDirectory(dir=destination) as folder:
        scratch = Path(folder)
        for i, url in enumerate(urls):
            save_media(url, scratch / f"part-{i}", 80 * 1024**2)
        # Force an expected output format even if the provider uses PNG or webp.
        if kind == "imagem":
            cmd = ["-i", "part-0", "-frames:v", "1", "result.jpg"]
        elif len(urls) > 1:
            (scratch / "parts.txt").write_text("".join(f"file part-{idx}\n" for idx in range(len(urls))))
            cmd = ["-f", "concat", "-safe", "1", "-i", "parts.txt", "-c", "copy",
                   "-movflags", "+faststart", "result.mp4"]
        else:
            cmd = ["-i", "part-0", "-c", "copy", "-movflags", "+faststart", "result.mp4"]
        try:
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file", *cmd],
                           cwd=scratch, timeout=90, check=True, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            raise Problem("Não foi possível montar o arquivo final.") from None
        (scratch / ("result." + ext)).replace(target)
    return "/studio/generations/media/" + row["id"]


def advance(queue, row, settings, key):
    data = json.loads(row["request"])
    label, kind, endpoint, *_ = MODELS[data["model_id"]]
    parts = json.loads(row["parts"])
    state = json.loads(row["provider"] or '{"urls":[]}')
    index = len(state["urls"])
    if not state.get("status_url"):
        with queue.connect(True) as db:
            db.execute("UPDATE studio_generations SET status='submitting' WHERE id=?", (row["id"],))
        reply = provider_call(f"{QUEUE_URL}/{endpoint}", key, provider_payload(data, parts[index], index, len(parts)))
        for url in ("status_url", "response_url"):
            if validate(reply.get(url, "")).hostname != "queue.fal.run":
                raise Problem("A fal.ai não retornou os endereços da fila.")
            state[url] = reply[url]
        with queue.connect(True) as db:
            db.execute("UPDATE studio_generations SET status='running', provider=? WHERE id=?",
                       (json.dumps(state), row["id"]))
        return
    current = provider_call(state["status_url"], key)
    if current.get("status") in {"IN_QUEUE", "IN_PROGRESS"}:
        return
    if current.get("status") != "COMPLETED" or current.get("error"):
        raise Problem("A fal.ai informou falha na geração.")
    result = provider_call(state["response_url"], key)
    url = result["images"][0]["url"] if kind == "imagem" else result["video"]["url"]
    validate(url)
    state["urls"].append(url)
    for field in ("status_url", "response_url"):
        state.pop(field, None)
    if len(state["urls"]) < len(parts):
        with queue.connect(True) as db:
            db.execute("UPDATE studio_generations SET status='queued', provider=? WHERE id=?",
                       (json.dumps(state), row["id"]))
        return
    with media_tools.MEDIA_SLOT:
        output = finish_media(row, state["urls"], kind, settings.data)
    complete(queue, row, label, output)


def complete(queue, row, label, output, free=False):
    with queue.connect(True) as db:
        db.execute("UPDATE studio_generations SET status='completed', output=? WHERE id=?", (output, row["id"]))
        cost = "Workers AI" if free else f"estimativa US$ {row['estimate']:.3f}"
        db.execute("INSERT INTO studio_ledger(kind,detail,created) VALUES (?,?,?)",
                   ("midia", f"{label} · {cost}", time.time()))
        queue.studio_notice(db, "midia_pronta", "Geração concluída", label)


def generate_with_cloudflare(queue, row, settings, credentials, data):
    if not credentials:
        raise Problem("Conecte a Cloudflare Workers AI para usar este modelo.")
    with queue.connect(True) as db:
        db.execute("UPDATE studio_generations SET status='submitting' WHERE id=?", (row["id"],))
    image = cloudflare_media.generate_image(credentials, data["prompt"])
    with queue.connect(True) as db:
        db.execute("UPDATE studio_generations SET status='running' WHERE id=?", (row["id"],))
    cloudflare_media.save_result(image, data["model_id"], data["ratio"], data["duration"],
                                 settings.data / "generations" / row["id"])
    complete(queue, row, MODELS[data["model_id"]][0], "/studio/generations/media/" + row["id"], free=True)


def worker(queue, settings, stop):
    key = os.getenv("FAL_KEY")
    cloudflare_key = cloudflare_media.Credentials.configured()
    if not (key or cloudflare_key):
        return
    schema(queue)
    with queue.connect(True) as db:
        db.execute("UPDATE studio_generations SET status='failed', error=? WHERE status='submitting'",
                   ("O servidor reiniciou durante o envio. Consulte o histórico do provedor antes de tentar novamente.",))
        db.execute("""
            UPDATE studio_generations SET status='failed', error=?
            WHERE status='running' AND json_extract(request, '$.model_id') IN ('flux-cloudflare','animacao-cloudflare')
        """, ("O servidor reiniciou antes de salvar a geração.",))
    while not stop.wait(4):
        with queue.connect() as db:
            row = db.execute("SELECT * FROM studio_generations WHERE status IN ('queued','running') "
                             "ORDER BY created LIMIT 1").fetchone()
        if not row or (row["status"] == "queued" and not row["provider"] and queue.studio_paused()):
            continue
        try:
            if time.time() - row["created"] > 3600:
                raise Problem("A geração ultrapassou o tempo de espera. Consulte o histórico no provedor.")
            data = json.loads(row["request"])
            if data["model_id"] in cloudflare_media.MODEL_IDS:
                generate_with_cloudflare(queue, row, settings, cloudflare_key, data)
            elif not key:
                raise Problem("Conecte a fal.ai para usar este modelo.")
            else:
                advance(queue, row, settings, key)
        except Exception as exc:
            message = str(exc) if isinstance(exc, Problem) else "Não foi possível montar o resultado da geração."
            LOG.warning("Geração %s falhou (%s).", row["id"], type(exc).__name__)
            with queue.connect(True) as db:
                db.execute("UPDATE studio_generations SET status='failed', error=? WHERE id=?", (message, row["id"]))
                queue.studio_notice(db, "midia_falhou", "Falha na geração", message)
