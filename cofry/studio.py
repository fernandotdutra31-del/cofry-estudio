import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .config import Problem
from .network import validate
from .schema import Edit, Job, Options

ASSETS = Path(__file__).with_name("assets")
ORIGIN = "https://upload.cofry.invalid/"
CALLBACK = "https://studio.cofry.invalid/"
COOKIE = "cofry_session"
router = APIRouter(prefix="/studio")


def signature(key, expires):
    return hmac.new(key.encode(), f"studio:{expires}".encode(), hashlib.sha256).hexdigest()


def authenticated(request):
    token = request.cookies.get(COOKIE, "")
    try:
        expires, digest = token.split(".", 1)
        return int(expires) > time.time() and hmac.compare_digest(
            digest.encode(), signature(request.app.state.settings.studio_key, expires).encode()
        )
    except ValueError:
        return False


def guard(request):
    if not authenticated(request):
        raise HTTPException(401, "Entre com a chave do estúdio.")
    return request.app.state


def studio_job(job):
    return job.callback_url == CALLBACK


@router.get("")
def home():
    return FileResponse(ASSETS / "index.html", headers={"Cache-Control": "no-cache"})


@router.get("/assets/{asset}")
def asset(asset: str):
    if asset not in {"app.js", "style.css", "manifest.webmanifest", "sw.js", "icon.png", "icon-mask.png"}:
        raise HTTPException(404)
    return FileResponse(ASSETS / asset, headers={"Service-Worker-Allowed": "/studio"})


@router.post("/session")
async def login(request: Request):
    settings = request.app.state.settings
    key = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(key.encode(), settings.studio_key.encode()):
        raise HTTPException(401, "Chave inválida.")
    expires = int(time.time()) + 86400
    response = JSONResponse({"ok": True})
    response.set_cookie(
        COOKIE,
        f"{expires}.{signature(key, expires)}",
        max_age=86400,
        httponly=True,
        secure=settings.studio_cookie_secure or request.url.scheme == "https",
        samesite="strict",
        path="/studio",
    )
    return response


@router.get("/session")
def session(request: Request):
    state = guard(request)
    return {"ok": True, "max_mb":state.settings.max_bytes // 1024**2,
            "youtube":state.settings.youtube, "modo": "ia" if state.settings.llm_key else "local"}


@router.delete("/session")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/studio")
    return response


@router.get("/projects")
def projects(request: Request):
    state = guard(request)
    with state.queue.connect() as db:
        rows = db.execute("""
            SELECT id, result FROM jobs
            WHERE json_extract(request, '$.callback_url')=? AND state NOT IN ('deleting','deleted')
            ORDER BY created DESC LIMIT 100
        """, (CALLBACK,)).fetchall()
    return [{"job_id": row["id"], **json.loads(row["result"])} for row in rows]


@router.post("/projects", status_code=202)
async def create(request: Request):
    state = guard(request)
    job_id = uuid4().hex
    try:
        options = Options.model_validate_json(request.headers.get("X-Studio-Options", "{}"))
    except ValueError:
        raise HTTPException(422, "Revise as opções e os tempos informados.") from None
    content_type = request.headers.get("Content-Type", "")
    if content_type.startswith("application/json"):
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 20000:
                    raise ValueError
                raw.extend(chunk)
            url = json.loads(raw)["url"]
            if not isinstance(url, str) or len(url) > 12000:
                raise ValueError
            validate(url)
            if url.startswith(ORIGIN):
                raise ValueError
        except (ValueError, Problem, KeyError, TypeError):
            raise HTTPException(422, "Informe um link HTTPS válido.") from None
    else:
        root = state.settings.data / "studio-inbox"
        root.mkdir(exist_ok=True)
        target = root / job_id
        size = 0
        try:
            # Use bounded chunks and keep ownership until the queue transaction commits.
            with target.open("xb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > state.settings.max_bytes:
                        raise HTTPException(413, "O vídeo excede o limite de tamanho.")
                    stream.write(chunk)
            if not size:
                raise HTTPException(422, "Escolha um arquivo de vídeo.")
            url = ORIGIN + job_id
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    try:
        state.queue.submit(Job(job_id=job_id, video_url=url, options=options, callback_url=CALLBACK))
    except BaseException:
        if url == ORIGIN + job_id:
            target.unlink(missing_ok=True)
        raise
    return {"job_id":job_id,"status":"queued"}


class LocalStorage:
    def __init__(self, data):
        self.root = data / "studio-library"
        self.root.mkdir(exist_ok=True)

    def put(self, job, clip, video, cover, check, written):
        revision = uuid4().hex
        urls = {}
        for field, source in (("url_mp4", video), ("url_capa", cover)):
            check()
            key = f"studio/{job}/{clip}/{revision}/{source.name}"
            written(key)
            destination = self.root / key
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            urls[field] = "/studio/media/" + key
        return urls

    def delete(self, keys):
        for key in keys:
            path = self.root / key
            path.unlink(missing_ok=True)
            directory = path.parent
            while directory != self.root:
                try:
                    directory.rmdir()
                except OSError:
                    break
                directory = directory.parent


@router.get("/media/{key:path}")
def media(key: str, request: Request, download: bool = False):
    state = guard(request)
    root = state.settings.data / "studio-library"
    path = (root / key).resolve()
    if not path.is_relative_to(root) or path.name not in {"clip.mp4", "cover.jpg"} or not path.is_file():
        raise HTTPException(404, "Arquivo indisponível.")
    return FileResponse(path, filename=path.name if download else None,
                        headers={"Cache-Control": "private, max-age=0"})


@router.get("/projects/{job_id}")
def result(job_id: str, request: Request):
    row = project_row(guard(request), job_id)
    return json.loads(row["result"])


def project_row(state, job_id):
    row = state.queue.get(job_id)
    if row is None or Job.model_validate_json(row["request"]).callback_url != CALLBACK:
        raise HTTPException(404, "Projeto não encontrado.")
    return row


@router.delete("/projects/{job_id}", status_code=202)
def cancel(job_id: str, request: Request):
    state = guard(request)
    project_row(state, job_id)
    state.queue.cancel(job_id)
    return {"ok": True}


@router.post("/projects/{job_id}/cuts/{cut_id}", status_code=202)
def rerender(job_id: str, cut_id: str, request: Request, edit: Edit):
    state = guard(request)
    project_row(state, job_id)
    code = state.queue.rerender(job_id, cut_id, edit)
    if code != 202:
        raise HTTPException(code, {404:"Corte não encontrado.", 409:"O projeto está em processamento.",
                                   422:"Revise os tempos do corte e das legendas."}[code])
    return {"ok":True}
