import hashlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import quote, unquote, urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field
from urllib3.exceptions import HTTPError

from .config import Problem
from .network import read_limited, request as outbound, validate
from .schema import Model
from .studio import guard, project_row

router = APIRouter(prefix="/studio/social")
BRASILIA = ZoneInfo("America/Sao_Paulo")
PROVIDER = "https://zernio.com/api"
SUPPORTED = ("instagram", "tiktok", "youtube")
STATUSES = {"draft", "pending_approval", "scheduled", "publishing", "published", "partial", "failed", "cancelled"}
EDITABLE = {"scheduled", "failed", "draft"}
DEMO_ACCOUNTS = [
    {"id": "demo-instagram", "platform": "instagram", "name": "Instagram Demo", "active": True},
    {"id": "demo-tiktok", "platform": "tiktok", "name": "TikTok Demo", "active": True},
    {"id": "demo-youtube", "platform": "youtube", "name": "YouTube Demo", "active": True},
]
POST_FIELDS = ("id", "project_id", "cut_id", "account_id", "platform", "scheduled_for", "caption", "status")
IDENT = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")]
CAPTION = Annotated[str, Field(min_length=1, max_length=2200)]
WRITES = threading.Lock()


class CreatePost(Model):
    project_id: IDENT
    cut_id: IDENT
    account_id: IDENT
    scheduled_for: datetime
    caption: CAPTION
    consent: bool = Field(default=False, strict=True)
    idempotency_key: IDENT


class EditPost(Model):
    scheduled_for: datetime | None = None
    caption: CAPTION | None = None


def moment(value):
    return value.replace(tzinfo=BRASILIA) if value.tzinfo is None else value.astimezone(BRASILIA)


def future(value, margin=0):
    if value <= datetime.now(BRASILIA) + timedelta(seconds=margin):
        raise HTTPException(422, "Escolha um horário futuro com antecedência suficiente.")


def provider_problem(code):
    if code == 402:
        return "O provedor interrompeu o agendamento por saldo ou cota insuficiente."
    if code in (401, 403):
        return "Revise a chave do Zernio e as permissões da conta conectada."
    if code == 429:
        return "O Zernio atingiu o limite de solicitações. Tente novamente em instantes."
    if code == 409:
        return "O Zernio já recebeu este conteúdo ou está processando o agendamento."
    if code in (400, 422):
        return "O Zernio recusou os dados do agendamento. Revise a conta e o vídeo."
    if code == 404:
        return "A publicação não está mais disponível no Zernio."
    return "O Zernio não respondeu como esperado. Tente novamente."


class ProviderFailure(HTTPException):
    def __init__(self, code, payload=None):
        self.provider_status = code
        self.payload = payload or {}
        super().__init__(code if code in (409, 422) else 502, provider_problem(code))


class Zernio:
    def __init__(self, key):
        self.key = key

    def call(self, method, path, data=None, key=None):
        headers = {"Authorization": "Bearer " + self.key, "Accept": "application/json",
                   "Accept-Encoding": "identity"}
        body = json.dumps(data).encode() if data is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key:
            headers["Idempotency-Key"] = key
        try:
            with outbound(PROVIDER + path, method, body, headers, read_timeout=15) as response:
                code = response.status
                raw = read_limited(response, 1_000_000)
        except (OSError, HTTPError, Problem) as exc:
            raise ProviderFailure(502) from exc
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise ProviderFailure(code if code >= 400 else 502) from exc
        if not 200 <= code < 300:
            raise ProviderFailure(code, payload if isinstance(payload, dict) else {})
        if not isinstance(payload, dict):
            raise ProviderFailure(502)
        return payload

    def accounts(self):
        accounts = {}
        cursor = None
        for _ in range(10):
            params = {"limit": 100, "platforms": ",".join(SUPPORTED)}
            if cursor:
                params["cursor"] = cursor
            reply = self.call("GET", "/v1/accounts?" + urlencode(params))
            rows = reply.get("accounts")
            if not isinstance(rows, list):
                raise ProviderFailure(502)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                account_id, platform = row.get("_id") or row.get("id"), row.get("platform")
                if not isinstance(account_id, str) or platform not in SUPPORTED:
                    continue
                username = row.get("username")
                accounts[account_id] = {
                    "id": account_id, "platform": platform,
                    "name": row.get("displayName") or username or platform,
                    "active": (
                        row.get("isActive", True) is not False and row.get("isConnected", True) is not False
                        and row.get("needsReconnect") is not True
                        and row.get("status") not in {"disconnected", "inactive", "expired"}
                        and row.get("connectionStatus") not in {"disconnected", "inactive", "expired"}
                    ),
                    "username": username, "avatar": row.get("profilePicture"),
                }
            pagination = reply.get("pagination")
            cursor = pagination.get("nextCursor") if isinstance(pagination, dict) else None
            if not cursor:
                return list(accounts.values())
        raise ProviderFailure(502)

    def post(self, post_id):
        return provider_post(self.call("GET", "/v1/posts/" + quote(post_id, safe="")))

    def find(self, external_id):
        value = self.call("GET", "/v1/posts?" + urlencode({"externalId": external_id, "limit": 10})).get("posts", [])
        if not isinstance(value, list):
            raise ProviderFailure(502)
        return next((post for post in value if isinstance(post, dict) and post.get("externalId") == external_id), None)

    def upload(self, file, post_id):
        length = file.stat().st_size
        reply = self.call("POST", "/v1/media/presign", {
            "filename": f"cofry-{post_id}.mp4", "contentType": "video/mp4", "size": length,
        })
        upload_url = reply.get("uploadUrl")
        public_url = reply.get("publicUrl")
        try:
            if not isinstance(upload_url, str) or not isinstance(public_url, str):
                raise ProviderFailure(502)
            validate(upload_url)
            validate(public_url)
            with file.open("rb") as stream:
                with outbound(upload_url, "PUT", stream, {
                    "Content-Type": "video/mp4", "Content-Length": str(length),
                }, read_timeout=30) as response:
                    if response.status not in (200, 201, 204):
                        raise ProviderFailure(response.status)
        except (OSError, HTTPError, Problem) as exc:
            raise HTTPException(502, "Não foi possível transferir o vídeo ao Zernio.") from exc
        return public_url


def provider_post(value):
    post = value.get("post", value)
    if not isinstance(post, dict):
        raise ProviderFailure(502)
    return post


def provider_status(raw, default):
    return raw if isinstance(raw, str) and raw in STATUSES else default


def social(request):
    state = guard(request)
    key = os.getenv("ZERNIO_API_KEY", "").strip()
    with state.queue.connect(True) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS studio_social_posts (
                id TEXT PRIMARY KEY, mode TEXT NOT NULL, dedupe TEXT NOT NULL,
                request_hash TEXT NOT NULL, project_id TEXT NOT NULL, cut_id TEXT NOT NULL,
                account_id TEXT NOT NULL, platform TEXT NOT NULL, scheduled_for TEXT NOT NULL,
                day TEXT NOT NULL, caption TEXT NOT NULL, status TEXT NOT NULL,
                title TEXT NOT NULL, provider_id TEXT, media_url TEXT, media_expires TEXT,
                created REAL NOT NULL, synced REAL NOT NULL DEFAULT 0, submitted INTEGER NOT NULL DEFAULT 0,
                UNIQUE(mode, dedupe)
            )
        """)
        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS studio_social_project_day
            ON studio_social_posts(mode, project_id, account_id, day) WHERE status != 'cancelled'
        """)
    return state, "real" if key else "demo", Zernio(key) if key else None


def result(row):
    return {field: row[field] for field in POST_FIELDS}


def stamp():
    return datetime.now(timezone.utc).timestamp()


def change(state, post_id, **values):
    fields = ", ".join(f"{field}=?" for field in values)
    with state.queue.connect(True) as db:
        before = db.execute("SELECT status FROM studio_social_posts WHERE id=?", (post_id,)).fetchone()
        db.execute(f"UPDATE studio_social_posts SET {fields} WHERE id=?", (*values.values(), post_id))
        row = db.execute("SELECT * FROM studio_social_posts WHERE id=?", (post_id,)).fetchone()
        if before and row["mode"] == "real" and row["status"] != before["status"]:
            if row["status"] == "published":
                state.queue.studio_notice(db, "post_publicado", "Post publicado", row["title"])
            elif row["status"] in {"failed", "partial"}:
                state.queue.studio_notice(db, "post_falhou", "Falha na publicação", row["title"])
        return row


def row_for(state, mode, post_id):
    with state.queue.connect() as db:
        row = db.execute("SELECT * FROM studio_social_posts WHERE id=? AND mode=?", (post_id, mode)).fetchone()
    if not row:
        raise HTTPException(404, "Agendamento não encontrado.")
    return row


def ensure_unique(db, mode, project, account, day, exclude=""):
    row = db.execute("""
        SELECT 1 FROM studio_social_posts
        WHERE mode=? AND project_id=? AND account_id=? AND day=? AND id!=? AND status!='cancelled'
    """, (mode, project, account, day, exclude)).fetchone()
    if row:
        raise HTTPException(409, "Este projeto já tem um agendamento nesta conta e nesta data.")


def lookup_clip(state, data, mode):
    if mode == "demo" and data.project_id == "demo" and data.cut_id == "demo":
        return {"titulo": "Corte de exemplo", "url_mp4": ""}
    row = project_row(state, data.project_id)
    if row["state"] != "completed":
        raise HTTPException(422, "Aguarde a conclusão do projeto para agendar.")
    cuts = json.loads(row["result"])["cortes"]
    cut = next((item for item in cuts if item["id"] == data.cut_id), None)
    if not cut or not cut.get("url_mp4"):
        raise HTTPException(404, "Corte não encontrado.")
    return cut


def local_file_or_url(state, value):
    prefix = "/studio/media/"
    if value.startswith(prefix):
        root = state.settings.data / "studio-library"
        file = (root / value.removeprefix(prefix)).resolve()
        if not file.is_relative_to(root) or file.name != "clip.mp4" or not file.is_file():
            raise HTTPException(422, "O arquivo do corte está indisponível.")
        return file
    try:
        validate(value)
    except Problem as exc:
        raise HTTPException(422, "O vídeo precisa estar disponível em um endereço HTTPS público.") from exc
    return value


def media_for(state, row, provider, source, schedule):
    if row["media_url"]:
        ensure_media_expiry(row, schedule)
        return row
    if isinstance(source, str):
        public = os.getenv("STORAGE_PUBLIC_BASE_URL", "").rstrip("/")
        expires = None
        if public and source.startswith(public + "/"):
            with state.queue.connect() as db:
                stored = db.execute(
                    "SELECT expires FROM uploads WHERE object_key=?", (unquote(source[len(public) + 1:]),)
                ).fetchone()
            if not stored:
                raise HTTPException(422, "O link do vídeo já não está disponível no armazenamento.")
            expires = datetime.fromtimestamp(stored["expires"], BRASILIA)
            if schedule >= expires:
                raise HTTPException(422, "Escolha um horário dentro da validade do vídeo enviado.")
        return change(state, row["id"], media_url=source, media_expires=expires.isoformat() if expires else None)
    expires = datetime.now(BRASILIA) + timedelta(days=7) - timedelta(hours=1)
    if schedule >= expires:
        raise HTTPException(422, "Agende um vídeo do estúdio com até seis dias de antecedência.")
    return change(state, row["id"], media_url=provider.upload(source, row["id"]), media_expires=expires.isoformat())


def ensure_media_expiry(row, schedule):
    if row["media_expires"] and schedule >= datetime.fromisoformat(row["media_expires"]):
        raise HTTPException(422, "Escolha um horário dentro da validade do vídeo enviado.")


def target(platform, account, caption, title):
    item = {"platform": platform, "accountId": account, "customContent": caption}
    if platform == "youtube":
        item["platformSpecificData"] = {"title": title[:100]}
    if platform == "tiktok":
        item["platformSpecificData"] = {"tiktokSettings": {
            "privacyLevel": "PUBLIC_TO_EVERYONE", "allowComment": True,
            "allowDuet": True, "allowStitch": True,
            "contentPreviewConfirmed": True, "expressConsentGiven": True,
        }}
    return item


def real_create(state, row, provider, source, schedule):
    # Check the provider's external ID before retrying a request beyond its 24-hour idempotency window.
    if stamp() - row["created"] > 86_400:
        existing = provider.find(row["id"])
        if existing:
            return finish_create(state, row, existing)
    row = media_for(state, row, provider, source, schedule)
    # A past scheduledFor causes immediate publishing upstream. Recheck after the media upload.
    future(schedule, margin=90)
    payload = {
        "title": row["title"], "content": row["caption"], "externalId": row["id"],
        "scheduledFor": row["scheduled_for"], "timezone": "America/Sao_Paulo",
        "mediaItems": [{"type": "video", "url": row["media_url"]}],
        "platforms": [target(row["platform"], row["account_id"], row["caption"], row["title"])],
    }
    change(state, row["id"], submitted=1)
    try:
        post = provider_post(provider.call("POST", "/v1/posts", payload, key=row["id"]))
    except ProviderFailure as exc:
        if exc.provider_status != 409:
            raise
        post = provider.find(row["id"])
        if not post:
            raise exc
    return finish_create(state, row, post)


def finish_create(state, row, post):
    provider_id = post.get("_id") or post.get("id") or post.get("postId")
    if not isinstance(provider_id, str) or not provider_id:
        raise ProviderFailure(502)
    return change(state, row["id"], provider_id=provider_id,
                  status=provider_status(post.get("status"), "scheduled"), synced=stamp())


def sync_post(state, mode, provider, row):
    if mode == "demo":
        if row["status"] == "scheduled" and datetime.fromisoformat(row["scheduled_for"]) <= datetime.now(BRASILIA):
            return change(state, row["id"], status="published")
        return row
    if not row["provider_id"]:
        if row["status"] == "submitting" and row["submitted"]:
            found = provider.find(row["id"])
            if found:
                return finish_create(state, row, found)
        return row
    try:
        post = provider.post(row["provider_id"])
    except ProviderFailure as exc:
        if exc.provider_status == 404:
            return change(state, row["id"], status="cancelled", synced=stamp())
        raise
    return change(state, row["id"], status=provider_status(post.get("status"), row["status"]), synced=stamp())


@router.get("")
def get_social(request: Request):
    state, mode, provider = social(request)
    accounts = provider.accounts() if provider else DEMO_ACCOUNTS
    warning = None
    with WRITES:
        with state.queue.connect(True) as db:
            if mode == "demo":
                db.execute("""
                    UPDATE studio_social_posts SET status='published'
                    WHERE mode='demo' AND status='scheduled' AND scheduled_for<=?
                """, (datetime.now(BRASILIA).isoformat(),))
            rows = db.execute("""
                SELECT * FROM studio_social_posts WHERE mode=? ORDER BY scheduled_for DESC, id LIMIT 200
            """, (mode,)).fetchall()
        if provider:
            candidates = sorted(
                (row for row in rows if row["status"] in {"submitting", "scheduled", "publishing", "pending_approval"}
                 and stamp() - row["synced"] >= 60),
                key=lambda row: row["synced"],
            )[:3]
            updated = {}
            for row in candidates:
                try:
                    updated[row["id"]] = sync_post(state, mode, provider, row)
                    if updated[row["id"]]["synced"] == row["synced"]:
                        updated[row["id"]] = change(state, row["id"], synced=stamp())
                except ProviderFailure:
                    warning = "Alguns status não puderam ser atualizados no Zernio."
                    updated[row["id"]] = change(state, row["id"], synced=stamp())
            rows = [updated.get(row["id"], row) for row in rows]
    payload = {"mode": mode, "accounts": accounts, "posts": [result(row) for row in rows]}
    if warning:
        payload["warning"] = warning
    return payload


@router.post("/posts", status_code=201)
def create_post(data: CreatePost, request: Request):
    state, mode, provider = social(request)
    schedule = moment(data.scheduled_for).replace(microsecond=0)
    schedule_iso = schedule.isoformat()
    day = schedule.date().isoformat()
    digest = hashlib.sha256(
        data.model_copy(update={"scheduled_for": schedule}).model_dump_json().encode()
    ).hexdigest()
    with WRITES:
        with state.queue.connect() as db:
            existing = db.execute(
                "SELECT * FROM studio_social_posts WHERE mode=? AND dedupe=?", (mode, data.idempotency_key)
            ).fetchone()
        if existing:
            if existing["request_hash"] != digest:
                raise HTTPException(409, "Esta chave de agendamento já foi usada com outros dados.")
            if existing["status"] != "submitting":
                return result(existing)
            # A prior request may have been accepted even if its response was lost.
            found = provider.find(existing["id"]) if existing["submitted"] else None
            if found:
                return result(finish_create(state, existing, found))

        future(schedule, margin=90 if provider else 0)
        account = next((account for account in (provider.accounts() if provider else DEMO_ACCOUNTS)
                        if account["id"] == data.account_id and account["active"]), None)
        if not account:
            raise HTTPException(422, "Escolha uma conta conectada para agendar.")
        platform = account["platform"]
        if platform == "tiktok" and not data.consent:
            raise HTTPException(422, "Revise e autorize a publicação no TikTok.")
        cut = lookup_clip(state, data, mode)
        source = local_file_or_url(state, cut["url_mp4"]) if provider and (not existing or not existing["media_url"]) else None
        if existing:
            row = existing
        else:
            post_id = uuid4().hex
            with state.queue.connect(True) as db:
                ensure_unique(db, mode, data.project_id, data.account_id, day)
                db.execute("""
                    INSERT INTO studio_social_posts (
                      id,mode,dedupe,request_hash,project_id,cut_id,account_id,platform,
                      scheduled_for,day,caption,status,title,created
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (post_id, mode, data.idempotency_key, digest, data.project_id, data.cut_id,
                      data.account_id, platform, schedule_iso, day, data.caption,
                      "submitting" if provider else "scheduled", cut["titulo"], stamp()))
            row = row_for(state, mode, post_id)
        if provider:
            try:
                row = real_create(state, row, provider, source, schedule)
            except ProviderFailure as exc:
                if exc.provider_status in (400, 401, 402, 403, 404, 422) or exc.payload.get("code") == "duplicate_content":
                    change(state, row["id"], status="failed")
                raise
    return result(row)


@router.patch("/posts/{post_id}")
def edit_post(post_id: str, data: EditPost, request: Request):
    state, mode, provider = social(request)
    if data.caption is None and data.scheduled_for is None:
        raise HTTPException(422, "Informe uma nova legenda ou um novo horário.")
    with WRITES:
        row = row_for(state, mode, post_id)
        if row["status"] != "cancelled":
            row = sync_post(state, mode, provider, row)
        if row["status"] not in EDITABLE:
            raise HTTPException(409, "Este agendamento não está disponível para edição.")
        schedule = moment(data.scheduled_for).replace(microsecond=0) if data.scheduled_for else datetime.fromisoformat(row["scheduled_for"])
        future(schedule, margin=90 if provider else 0)
        ensure_media_expiry(row, schedule)
        caption = data.caption if data.caption is not None else row["caption"]
        day = schedule.date().isoformat()
        with state.queue.connect() as db:
            ensure_unique(db, mode, row["project_id"], row["account_id"], day, post_id)
        status = "scheduled"
        if provider:
            if not row["provider_id"]:
                raise HTTPException(409, "Cancele este agendamento com falha e crie um novo.")
            post = provider_post(provider.call("PUT", "/v1/posts/" + quote(row["provider_id"], safe=""), {
                "scheduledFor": schedule.isoformat(), "timezone": "America/Sao_Paulo",
                "content": caption, "platforms": [target(row["platform"], row["account_id"], caption, row["title"])],
                "isDraft": False,
            }))
            status = provider_status(post.get("status"), status)
        return result(change(state, post_id, scheduled_for=schedule.isoformat(), day=day,
                             caption=caption, status=status, synced=stamp()))


@router.delete("/posts/{post_id}")
def cancel_post(post_id: str, request: Request):
    state, mode, provider = social(request)
    with WRITES:
        row = row_for(state, mode, post_id)
        if row["status"] == "cancelled":
            return result(row)
        row = sync_post(state, mode, provider, row)
        if row["status"] == "submitting" and row["submitted"]:
            raise HTTPException(409, "Aguarde a confirmação do Zernio antes de cancelar.")
        if row["status"] in {"publishing", "published", "partial"}:
            raise HTTPException(409, "A publicação já foi iniciada ou enviada.")
        if provider and row["provider_id"]:
            try:
                provider.call("DELETE", "/v1/posts/" + quote(row["provider_id"], safe=""))
            except ProviderFailure as exc:
                if exc.provider_status != 404:
                    raise
        return result(change(state, post_id, status="cancelled", synced=stamp()))
