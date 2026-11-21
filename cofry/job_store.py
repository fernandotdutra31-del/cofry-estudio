import json
import sqlite3
import time
from contextlib import contextmanager


class Queue:
    def __init__(self, folder):
        self.path = folder / "jobs.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL,
                    action TEXT, edit TEXT, state TEXT NOT NULL, created REAL NOT NULL,
                    words TEXT, duration REAL, fingerprint TEXT
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, url TEXT NOT NULL,
                    body TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                    due REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    object_key TEXT PRIMARY KEY, job TEXT NOT NULL, committed INTEGER NOT NULL DEFAULT 0, expires REAL NOT NULL
                );
            """)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def submit(self, job):
        result = dict(status="queued", etapa="fila", progresso=0, erro=None, cortes=[])
        with self.connect(True) as db:
            db.execute("INSERT OR IGNORE INTO jobs VALUES (?, ?, ?, 'initial', NULL, 'queued', ?, NULL, NULL, NULL)",
                       (job.job_id, job.model_dump_json(), json.dumps(result), time.time()))
            row = self.get(job.job_id, db)
            if row["request"] != job.model_dump_json():
                return "conflict", None
            return row["state"], json.loads(row["result"])

    def get(self, job_id, db=None):
        if db is None:
            with self.connect() as connection:
                return self.get(job_id, connection)
        return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def rerender(self, job_id, clip_id, edit):
        with self.connect(True) as db:
            row = self.get(job_id, db)
            if not row or row["state"] in {"deleting", "deleted"}:
                return 404
            result = json.loads(row["result"])
            if not any(cut["id"] == clip_id for cut in result["cortes"]):
                return 404
            if row["state"] not in {"completed", "failed"}:
                return 409
            if edit.fim > row["duration"]:
                return 422
            if edit.legenda is not None and any(
                block.inicio < edit.inicio or block.fim > edit.fim for block in edit.legenda.blocos
            ):
                return 422
            result.update(status="queued", etapa="fila", progresso=0, erro=None)
            payload = json.dumps({"clip_id": clip_id, "edit": edit.model_dump()})
            db.execute("UPDATE jobs SET state='queued', result=?, action='rerender', edit=?, created=? WHERE id=?",
                       (json.dumps(result), payload, time.time(), job_id))
            return 202

    def cancel(self, job_id):
        with self.connect(True) as db:
            row = self.get(job_id, db)
            if row is None:
                return False
            if row["state"] not in {"deleting", "deleted"}:
                result = dict(status="cancelled", etapa="cancelado", progresso=0, erro=None, cortes=[])
                db.execute("""UPDATE jobs SET state='deleting', action='delete', result=?, words=NULL,
                           duration=NULL, fingerprint=NULL, edit=NULL WHERE id=?""", (json.dumps(result), job_id))
                db.execute("DELETE FROM outbox WHERE job=?", (job_id,))
            return True

    def recover(self):
        with self.connect(True) as db:
            for row in db.execute("SELECT id, result FROM jobs WHERE state='running'").fetchall():
                result = json.loads(row["result"])
                result.update(status="queued", etapa="fila", progresso=0, erro=None)
                db.execute("UPDATE jobs SET state='queued', result=? WHERE id=?", (json.dumps(result), row["id"]))

    def claim(self):
        with self.connect(True) as db:
            row = db.execute("""SELECT * FROM jobs WHERE state='queued' OR (state='deleting' AND created<=?)
                             ORDER BY state='deleting' DESC, created, rowid LIMIT 1""", (time.time(),)).fetchone()
            if row and row["state"] == "queued":
                db.execute("UPDATE jobs SET state='running' WHERE id=?", (row["id"],))
            return row

    def progress(self, job_id, stage, percent):
        with self.connect(True) as db:
            row = self.get(job_id, db)
            if row["state"] != "running":
                return
            result = json.loads(row["result"])
            result.update(status="running", etapa=stage, progresso=percent)
            db.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps(result), job_id))

    def finish(self, job, error, cuts=None, words=None, duration=None, fingerprint=None):
        with self.connect(True) as db:
            row = self.get(job.job_id, db)
            if row["state"] != "running":
                return False
            result = json.loads(row["result"])
            if error:
                result.update(status="failed", erro=error)
            else:
                result.update(status="completed", etapa="concluido", progresso=100, cortes=cuts)
                db.execute("UPDATE uploads SET committed=1 WHERE job=?", (job.job_id,))
            body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            db.execute("""UPDATE jobs SET state=?, result=?, words=COALESCE(?, words),
                       duration=COALESCE(?, duration), fingerprint=COALESCE(?, fingerprint),
                       action=NULL, edit=NULL WHERE id=?""",
                       (result["status"], body, json.dumps(words) if words is not None else None,
                        duration, fingerprint, job.job_id))
            if job.callback_url != "https://studio.cofry.invalid/":
                db.execute("INSERT INTO outbox(job,url,body,due) VALUES (?,?,?,?)",
                       (job.job_id, job.callback_url, body, time.time()))
            return True

    def record(self, job_id, key):
        with self.connect(True) as db:
            db.execute("INSERT INTO uploads(object_key,job,expires) VALUES (?,?,?)",
                       (key, job_id, time.time() + 31 * 86400))

    def keys(self, job_id, only_pending):
        with self.connect() as db:
            return [row[0] for row in db.execute(
                "SELECT object_key FROM uploads WHERE job=? AND (committed=0 OR ?=0)", (job_id, only_pending))]

    def expired(self):
        with self.connect() as db:
            return [row[0] for row in db.execute(
                "SELECT object_key FROM uploads WHERE expires<=? ORDER BY expires LIMIT 100", (time.time(),))]

    def remove_key(self, key):
        with self.connect(True) as db:
            db.execute("DELETE FROM uploads WHERE object_key=?", (key,))

    def deleted(self, job_id, retry):
        with self.connect(True) as db:
            if retry:
                db.execute("UPDATE jobs SET created=? WHERE id=?", (time.time() + 30, job_id))
            else:
                db.execute("UPDATE jobs SET state='deleted', action=NULL WHERE id=?", (job_id,))

    def next_delivery(self):
        with self.connect() as db:
            return db.execute("SELECT * FROM outbox WHERE due<=? ORDER BY seq LIMIT 1", (time.time(),)).fetchone()

    def delivered(self, row, ok):
        with self.connect(True) as db:
            if ok or row["attempt"] >= 2:
                db.execute("DELETE FROM outbox WHERE seq=?", (row["seq"],))
            else:
                db.execute("UPDATE outbox SET attempt=attempt+1, due=? WHERE seq=?",
                           (time.time() + 2 ** (row["attempt"] + 1), row["seq"]))
