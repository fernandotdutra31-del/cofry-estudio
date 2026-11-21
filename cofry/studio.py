import hashlib
import hmac
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from . import google_auth
from .config import Problem
from .network import validate
from .schema import Edit, Job, Options

ASSETS = Path(__file__).with_name("assets")
ORIGIN = "https://upload.cofry.invalid/"
CALLBACK = "https://studio.cofry.invalid/"
COOKIE = "cofry_session"
SESSION_TTL = 86_400
router = APIRouter(prefix="/studio")


def signature(key, expires):
    return hmac.new(key.encode(), f"studio:{expires}".encode(), hashlib.sha256).hexdigest()


def authentication_method(request):
    settings = request.app.state.settings
    token = request.cookies.get(COOKIE, "")
    try:
        if token.startswith("google."):
            _, expires, digest = token.split(".")
            config = google_auth.GoogleOAuth.configured()
            if config is None:
                return None
            signed = f"google:{config.client_id}:{config.owner}:{expires}"
            method = "google"
        else:
            expires, digest = token.split(".")
            signed = expires
            method = "key"
        if int(expires) > time.time() and hmac.compare_digest(
            digest.encode(), signature(settings.studio_key, signed).encode()
        ):
            return method
    except ValueError:
        pass
    return None


def guard(request):
    if authentication_method(request) is None:
        raise HTTPException(401, "Entre no estúdio.")
    return request.app.state


def require_generation_open(state):
    if state.queue.studio_paused():
        raise HTTPException(409, "As gerações foram pausadas pelo proprietário.")


def set_session(response, request, google=None):
    settings = request.app.state.settings
    expires = int(time.time()) + SESSION_TTL
    if google is None:
        token = f"{expires}.{signature(settings.studio_key, expires)}"
    else:
        digest = signature(settings.studio_key, f"google:{google.client_id}:{google.owner}:{expires}")
        token = f"google.{expires}.{digest}"
    response.set_cookie(
        COOKIE,
        token,
        max_age=SESSION_TTL,
        httponly=True,
        secure=google is not None or settings.studio_cookie_secure or request.url.scheme == "https",
        samesite="strict",
        path="/studio",
    )
    return response


def studio_job(job):
    return job.callback_url == CALLBACK


@router.get("")
def home():
    return FileResponse(ASSETS / "index.html", headers={"Cache-Control": "no-cache"})


@router.get("/assets/{asset}")
def asset(asset: str):
    if asset not in {"app.js", "social.js", "owner.js", "generation.js", "demo-image.jpg", "demo-video.mp4", "style.css", "manifest.webmanifest", "sw.js", "icon.png", "icon-mask.png"}:
        raise HTTPException(404)
    return FileResponse(ASSETS / asset, headers={"Service-Worker-Allowed": "/studio"})


@router.post("/session")
async def login(request: Request):
    settings = request.app.state.settings
    key = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(key.encode(), settings.studio_key.encode()):
        raise HTTPException(401, "Chave inválida.")
    return set_session(JSONResponse({"ok": True}), request)


@router.get("/session")
def session(request: Request):
    method = authentication_method(request)
    if method is None:
        raise HTTPException(401, "Entre no estúdio.")
    settings = request.app.state.settings
    return {
        "ok": True,
        "max_mb": settings.max_bytes // 1024**2,
        "youtube": settings.youtube,
        "modo": "ia" if settings.llm_key else "local",
        "strategy": method,
        "user": {"name": "Proprietário", "email": google_auth.owner_email(), "owner": True},
        "credits": {"unlimited": True},
    }


@router.delete("/session")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/studio")
    clear_google_state(response)
    return response


@router.get("/auth/options")
def auth_options():
    return {"google": google_auth.GoogleOAuth.configured() is not None, "key": True}


def auth_redirect(location):
    return RedirectResponse(location, status_code=303, headers={"Referrer-Policy": "no-referrer"})


def clear_google_state(response):
    response.delete_cookie(
        google_auth.STATE_COOKIE, path=google_auth.CALLBACK_PATH,
        secure=True, httponly=True, samesite="lax",
    )
    return response


def google_result(error=None):
    return clear_google_state(auth_redirect(f"/studio?auth={error}" if error else "/studio"))


@router.get("/auth/google")
def google_start(request: Request):
    config = google_auth.GoogleOAuth.configured()
    if config is None:
        return google_result("google_unavailable")
    redirect = urlsplit(config.redirect_uri)
    if request.url.hostname != redirect.hostname:
        return auth_redirect(redirect._replace(path="/studio/auth/google").geturl())

    key = request.app.state.settings.studio_key
    state, cookie = google_auth.create_state(key)
    response = auth_redirect(config.authorization_url(state, google_auth.code_verifier(key, state)))
    response.set_cookie(
        google_auth.STATE_COOKIE, cookie, max_age=google_auth.STATE_TTL,
        secure=True, httponly=True, samesite="lax", path=google_auth.CALLBACK_PATH,
    )
    return response


@router.get("/auth/google/callback")
def google_callback(request: Request):
    config = google_auth.GoogleOAuth.configured()
    if config is None:
        return google_result("google_unavailable")

    key = request.app.state.settings.studio_key
    state = request.query_params.get("state", "")
    cookie = request.cookies.get(google_auth.STATE_COOKIE, "")
    if (
        request.url.hostname != urlsplit(config.redirect_uri).hostname
        or not google_auth.verify_state(key, state, cookie)
    ):
        return google_result("google_invalid")
    if request.query_params.get("error"):
        return google_result("google_denied")
    code = request.query_params.get("code", "")
    if not 1 <= len(code) <= 4096:
        return google_result("google_invalid")

    try:
        config.check_owner(code, google_auth.code_verifier(key, state))
    except google_auth.GoogleDenied:
        return google_result("google_denied")
    except google_auth.GoogleRetry:
        return google_result("google_retry")
    return set_session(google_result(), request, google=config)


@router.get("/projects")
def projects(request: Request):
    state = guard(request)
    with state.queue.connect() as db:
        rows = db.execute("""
            SELECT j.id, j.result, j.duration, m.title, COALESCE(m.created,j.created) AS created_at,
                   (SELECT MIN(expires) FROM uploads WHERE job=j.id AND committed=1) AS expiry
            FROM jobs j LEFT JOIN studio_project_metadata m ON m.job=j.id
            WHERE json_extract(j.request, '$.callback_url')=? AND j.state NOT IN ('deleting','deleted')
            ORDER BY created_at DESC LIMIT 100
        """, (CALLBACK,)).fetchall()
    answer = []
    for row in rows:
        result = json.loads(row["result"])
        first = next(iter(result["cortes"]), {})
        answer.append({
            "job_id": row["id"], "title": row["title"] or first.get("titulo") or f"Projeto {row['id'][:8]}",
            "created_at": datetime.fromtimestamp(row["created_at"], UTC).isoformat(),
            "expires_at": datetime.fromtimestamp(row["expiry"], UTC).isoformat() if row["expiry"] else None,
            "thumbnail": first.get("url_capa"), "original_duration": row["duration"], **result,
        })
    return answer


@router.post("/projects", status_code=202)
async def create(request: Request):
    state = guard(request)
    require_generation_open(state)
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
        require_generation_open(state)
        title = unquote(request.headers.get("X-Studio-Title", "")).strip()
        title = "".join(char for char in title if char.isprintable())[:120]
        if not title:
            title = f"Vídeo {job_id[:8]}" if url.startswith(ORIGIN) else urlsplit(url).hostname
        state.queue.submit(Job(job_id=job_id, video_url=url, options=options, callback_url=CALLBACK),
                           studio_title=title)
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


class ProjectTitle(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(min_length=1, max_length=120)


@router.post("/projects/{job_id}/retry", status_code=202)
def retry_project(job_id: str, request: Request):
    state = guard(request)
    require_generation_open(state)
    row = project_row(state, job_id)
    if row["state"] != "failed":
        raise HTTPException(409, "Só é possível tentar novamente projetos com falha.")
    previous = Job.model_validate_json(row["request"])
    with state.queue.connect() as db:
        metadata = db.execute("SELECT title FROM studio_project_metadata WHERE job=?", (job_id,)).fetchone()
    new_id = uuid4().hex
    video_url = previous.video_url
    link = None
    if video_url == ORIGIN + job_id:
        root = state.settings.data / "studio-inbox"
        link = root / new_id
        try:
            link.hardlink_to(root / job_id)
        except FileNotFoundError:
            raise HTTPException(410, "O arquivo original expirou. Envie o vídeo novamente.") from None
        video_url = ORIGIN + new_id
    try:
        state.queue.submit(
            previous.model_copy(update={"job_id": new_id, "video_url": video_url}),
            studio_title=metadata["title"] if metadata else None,
        )
    except BaseException:
        if link is not None:
            link.unlink(missing_ok=True)
        raise
    return {"job_id": new_id, "status": "queued"}


@router.patch("/projects/{job_id}")
def rename_project(job_id: str, payload: ProjectTitle, request: Request):
    state = guard(request)
    with state.queue.connect(True) as db:
        row = state.queue.get(job_id, db)
        if (row is None or row["state"] in {"deleting", "deleted"}
                or not studio_job(Job.model_validate_json(row["request"]))):
            raise HTTPException(404, "Projeto não encontrado.")
        db.execute("""
            INSERT INTO studio_project_metadata(job,title,created) VALUES (?, ?, ?)
            ON CONFLICT(job) DO UPDATE SET title=excluded.title
        """, (job_id, payload.title, row["created"]))
    return {"title": payload.title}


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
    require_generation_open(state)
    project_row(state, job_id)
    code = state.queue.rerender(job_id, cut_id, edit)
    if code != 202:
        raise HTTPException(code, {404:"Corte não encontrado.", 409:"O projeto está em processamento.",
                                   422:"Revise os tempos do corte e das legendas."}[code])
    return {"ok":True}
