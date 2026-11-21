import fcntl
import hmac
import json
import logging
import shutil
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from .config import Problem, Settings
from .generation import router as generation_router, worker as generation_worker
from .media import check_youtube
from .network import validate
from .owner import deliver_push, router as owner_router
from .job_store import Queue
from .schema import Edit, Job
from .service import deliver, work
from .studio import CALLBACK, router as studio_router
from .social import router as social_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app):
    settings = Settings()
    if not settings.api_key or not settings.secret:
        raise RuntimeError("Configure PROCESSING_API_KEY e WEBHOOK_SECRET.")
    settings.data.mkdir(parents=True, exist_ok=True)
    with (settings.data / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Já existe um worker usando este volume.") from None
        for leftover in settings.data.glob("task-*"):
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover)
        queue = Queue(settings.data)
        queue.recover()
        for upload in (settings.data / "studio-inbox").glob("*"):
            if upload.is_file() and queue.get(upload.name) is None:
                upload.unlink(missing_ok=True)
        stop = threading.Event()
        app.state.settings, app.state.queue = settings, queue
        threads = [threading.Thread(target=target, args=(queue, settings, stop), daemon=True)
                   for target in (work, deliver, deliver_push, generation_worker)]
        if settings.youtube and settings.youtube_check_url:
            settings.youtube = False
            settings.youtube_status = "verificando"
            threads.append(threading.Thread(target=check_youtube, args=(settings, stop), daemon=True))
        for thread in threads:
            thread.start()
        try:
            yield
        finally:
            stop.set()
            for thread in threads:
                thread.join()


app = FastAPI(title="Cofry Processamento", version="1.0.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


app.include_router(studio_router)
app.include_router(social_router)
app.include_router(owner_router)
app.include_router(generation_router)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    path = request.scope["path"]
    studio = path == "/studio" or path.startswith("/studio/")
    public_page = request.method in {"GET", "HEAD"} and path == "/"
    if not studio and not public_page and (path != "/health" or request.method != "GET"):
        key = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(key.encode(), request.app.state.settings.api_key.encode()):
            return JSONResponse({"detail": "Chave de API inválida."}, status_code=401)
    if request.method == "POST" and path != "/studio/projects":
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 524288:
                return JSONResponse({"detail": "O corpo da solicitação excede 512 KiB."}, status_code=413)
        request._body = bytes(body)
    response = await call_next(request)
    if studio and path != "/studio" and not path.startswith("/studio/assets/"):
        response.headers["Cache-Control"] = "private, no-store"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse({"detail": "Dados da solicitação inválidos.", "campos": [
        {"campo": ".".join(map(str, error["loc"])), "tipo": error["type"]}
        for error in exc.errors()
    ]}, status_code=422)


@app.get("/health")
def health(request: Request):
    return {"ok": True, "url_download_habilitado": request.app.state.settings.youtube}


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def portal():
    return RedirectResponse("/studio")


@app.post("/jobs", status_code=202)
def submit(job: Job, request: Request):
    try:
        validate(job.video_url)
        validate(job.callback_url)
        if job.callback_url == CALLBACK:
            raise Problem("Informe um callback HTTPS externo.")
    except Problem as exc:
        raise HTTPException(422, str(exc)) from None
    state, result = request.app.state.queue.submit(job)
    if state == "conflict":
        raise HTTPException(409, "O job_id já foi usado com outros parâmetros.")
    return {"job_id": job.job_id, "status": result["status"]}


@app.get("/jobs/{job_id}")
def status(job_id: str, request: Request):
    row = request.app.state.queue.get(job_id)
    if row is None:
        raise HTTPException(404, "Job não encontrado.")
    return json.loads(row["result"])


@app.post("/jobs/{job_id}/cortes/{clip_id}/rerender", status_code=202)
def rerender(job_id: str, clip_id: str, edit: Edit, request: Request):
    code = request.app.state.queue.rerender(job_id, clip_id, edit)
    errors = {404: "Corte não encontrado.", 409: "O job já está em processamento.",
              422: "O intervalo do corte ou das legendas está fora do vídeo."}
    if code != 202:
        raise HTTPException(code, errors[code])
    return {"job_id": job_id, "corte_id": clip_id, "status": "queued"}


@app.delete("/jobs/{job_id}", status_code=202)
def cancel(job_id: str, request: Request):
    if not request.app.state.queue.cancel(job_id):
        raise HTTPException(404, "Job não encontrado.")
    return {"job_id": job_id, "status": "cancelled"}
