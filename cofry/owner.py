import json
import logging
import os
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from .config import Problem
from .network import validate
from .schema import Model
from .studio import guard

router = APIRouter(prefix="/studio/owner")
LOG = logging.getLogger(__name__)
PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")


class Pause(Model):
    paused: bool


class Notice(Model):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=500)


class PushKeys(Model):
    p256dh: str = Field(min_length=40, max_length=160, pattern=r"^[A-Za-z0-9_=-]+$")
    auth: str = Field(min_length=12, max_length=100, pattern=r"^[A-Za-z0-9_=-]+$")


class Subscription(Model):
    endpoint: str = Field(min_length=10, max_length=2048)
    keys: PushKeys


def push_config():
    public = os.getenv("VAPID_PUBLIC_KEY", "").strip()
    private = os.getenv("VAPID_PRIVATE_KEY", "").strip()
    contact = os.getenv("VAPID_SUBJECT", "").strip() or os.getenv("OWNER_EMAIL", "").strip()
    if not public or not private or not contact:
        return None
    return public, private, contact if ":" in contact else "mailto:" + contact


def endpoint_allowed(endpoint):
    try:
        validate(endpoint)
        host = urlsplit(endpoint).hostname or ""
    except (ValueError, Problem):
        return False
    return any(host == base or host.endswith("." + base) for base in PUSH_HOSTS)


def timestamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


@router.get("")
def overview(request: Request):
    state = guard(request)
    push = push_config()
    with state.queue.connect() as db:
        notices = db.execute("SELECT * FROM studio_notifications ORDER BY id DESC LIMIT 100").fetchall()
        unread = db.execute("SELECT COUNT(*) FROM studio_notifications WHERE read=0").fetchone()[0]
        ledger = db.execute("SELECT * FROM studio_ledger ORDER BY id DESC LIMIT 100").fetchall()
    return {
        "paused": state.queue.studio_paused(), "unread": unread,
        "notifications": [{"id": n["id"], "title": n["title"], "body": n["body"], "kind": n["kind"],
                           "created_at": timestamp(n["created"]), "read": bool(n["read"])} for n in notices],
        "ledger": [{"id": item["id"], "kind": item["kind"], "detail": item["detail"],
                    "created_at": timestamp(item["created"])} for item in ledger],
        "push": {"available": bool(push), "public_key": push[0] if push else None},
    }


@router.patch("")
def pause(payload: Pause, request: Request):
    state = guard(request)
    with state.queue.connect(True) as db:
        db.execute("INSERT INTO studio_owner_state(id,paused) VALUES (1,?) "
                   "ON CONFLICT(id) DO UPDATE SET paused=excluded.paused", (int(payload.paused),))
    return {"paused": payload.paused}


@router.post("/notifications", status_code=201)
def create_notice(payload: Notice, request: Request):
    state = guard(request)
    with state.queue.connect(True) as db:
        state.queue.studio_notice(db, "manual", payload.title, payload.body)
    return {"ok": True}


@router.post("/notifications/read")
def mark_read(request: Request):
    state = guard(request)
    with state.queue.connect(True) as db:
        db.execute("UPDATE studio_notifications SET read=1 WHERE read=0")
    return {"ok": True}


@router.post("/push", status_code=201)
def subscribe(payload: Subscription, request: Request):
    state = guard(request)
    if not push_config():
        raise HTTPException(409, "O Web Push ainda não foi configurado.")
    if not endpoint_allowed(payload.endpoint):
        raise HTTPException(422, "O provedor de notificações deste navegador não é compatível.")
    with state.queue.connect(True) as db:
        count = db.execute("SELECT COUNT(*) FROM studio_push_subscriptions WHERE endpoint!=?",
                           (payload.endpoint,)).fetchone()[0]
        if count >= 20:
            raise HTTPException(409, "O limite de dispositivos foi atingido.")
        db.execute("""
            INSERT INTO studio_push_subscriptions(endpoint,p256dh,auth) VALUES (?,?,?)
            ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth
        """, (payload.endpoint, payload.keys.p256dh, payload.keys.auth))
    return {"ok": True}


def deliver_push(queue, _settings, stop):
    while not stop.wait(2):
        config = push_config()
        if not config:
            continue
        from pywebpush import WebPushException, webpush

        with queue.connect(True) as db:
            db.execute("UPDATE studio_notifications SET push_pending=0 WHERE push_pending=1 AND created<?",
                       (time.time() - 600,))
            notice = db.execute("SELECT * FROM studio_notifications WHERE push_pending=1 ORDER BY id LIMIT 1").fetchone()
            if not notice:
                continue
            subscriptions = db.execute("SELECT * FROM studio_push_subscriptions LIMIT 20").fetchall()
            db.execute("UPDATE studio_notifications SET push_pending=0 WHERE id=?", (notice["id"],))
        _, private, subject = config
        for row in subscriptions:
            subscription = {"endpoint": row["endpoint"], "keys": {"auth": row["auth"], "p256dh": row["p256dh"]}}
            if stop.is_set() or not endpoint_allowed(row["endpoint"]):
                continue
            try:
                webpush(subscription_info=subscription, data=json.dumps({"body": notice["body"]}),
                        vapid_private_key=private, vapid_claims={"sub": subject}, ttl=600, timeout=8)
            except WebPushException as exc:
                if exc.response is not None and exc.response.status_code in (404, 410):
                    with queue.connect(True) as db:
                        db.execute("DELETE FROM studio_push_subscriptions WHERE endpoint=?", (row["endpoint"],))
                else:
                    LOG.warning("Web Push recusado. O aviso continua disponível no estúdio.")
            except Exception as exc:
                LOG.warning("Web Push indisponível (%s).", type(exc).__name__)
