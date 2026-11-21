import hashlib
import hmac
import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import media, selection
from .config import Cancelled, Problem
from .network import request
from .schema import Edit, Job
from .storage import Storage
from .studio import LocalStorage, ORIGIN, studio_job

LOG = logging.getLogger(__name__)
TRANSCRIBER = Path(__file__).with_name("transcribe.py")


def clean_objects(queue, settings, job_id, only_pending):
    keys = queue.keys(job_id, only_pending)
    if keys:
        for key in keys:
            storage = LocalStorage(settings.data) if key.startswith("studio/") else Storage()
            storage.delete([key])
            queue.remove_key(key)


def work(queue, settings, stop):
    next_sweep = 0
    while not stop.is_set():
        try:
            if time.monotonic() >= next_sweep:
                next_sweep = time.monotonic() + 60
                try:
                    cutoff = time.time() - 31 * 86400
                    for source in (settings.data / "studio-inbox").glob("*"):
                        if source.is_file() and source.stat().st_mtime < cutoff:
                            active = queue.get(source.name)
                            if active is None or active["state"] not in {"queued", "running"}:
                                source.unlink(missing_ok=True)
                    expired = queue.expired()
                    if expired:
                        for key in expired:
                            storage = LocalStorage(settings.data) if key.startswith("studio/") else Storage()
                            storage.delete([key])
                            queue.remove_key(key)
                except Exception:
                    LOG.warning("Falha ao remover arquivos vencidos. Nova tentativa agendada.")
            row = queue.claim()
            if row is None:
                stop.wait(0.35)
            elif row["action"] == "delete":
                try:
                    clean_objects(queue, settings, row["id"], False)
                    (settings.data / "studio-inbox" / row["id"]).unlink(missing_ok=True)
                except Exception:
                    LOG.warning("Não foi possível apagar os arquivos de %s. Nova tentativa agendada.", row["id"])
                    queue.deleted(row["id"], retry=True)
                else:
                    queue.deleted(row["id"], retry=False)
            else:
                process(row, queue, settings, stop)
        except Exception as exc:
            LOG.error("Falha na fila de processamento (%s).", type(exc).__name__)
            # An infrastructure error can interrupt the durable transition.
            try:
                queue.recover()
            except Exception:
                LOG.error("O banco da fila está indisponível.")
            stop.wait(2)


def process(row, queue, settings, stop):
    job = Job.model_validate_json(row["request"])
    job_id = job.job_id

    def check():
        if stop.is_set() or queue.get(job_id)["state"] != "running":
            raise Cancelled

    def progress(stage, percent):
        check()
        LOG.info("job=%s etapa=%s progresso=%s", job_id, stage, percent)
        queue.progress(job_id, stage, percent)

    error, clips, words, duration, fingerprint = None, None, None, None, None
    try:
        clean_objects(queue, settings, job_id, True)
        progress("baixando", 4)
        with tempfile.TemporaryDirectory(prefix="task-", dir=settings.data) as directory:
            folder = Path(directory)
            if studio_job(job) and job.video_url == ORIGIN + job_id:
                source = folder / "source.media"
                try:
                    source.hardlink_to(settings.data / "studio-inbox" / job_id)
                except FileNotFoundError:
                    raise Problem("O arquivo original do projeto está indisponível.") from None
            else:
                source = media.download(job.video_url, folder, settings, check)
            progress("validando", 12)
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    check()
                    digest.update(chunk)
            fingerprint = digest.hexdigest()
            duration = media.probe(source, settings, check)
            if job.options.trecho_inicio >= duration:
                raise Problem("O início solicitado está fora do vídeo.")
            edit = None
            if row["action"] == "rerender":
                instruction = json.loads(row["edit"])
                edit = Edit.model_validate(instruction["edit"])
                if edit.fim > duration or fingerprint != row["fingerprint"]:
                    raise Problem("O vídeo original mudou desde o primeiro processamento.")
                words = json.loads(row["words"])
                clips = json.loads(row["result"])["cortes"]
                cut = next(cut for cut in clips if cut["id"] == instruction["clip_id"])
                cut.update(inicio=edit.inicio, fim=edit.fim, duracao=round(edit.fim - edit.inicio, 3))
                if isinstance(edit.gancho, str):
                    cut["gancho"] = edit.gancho
                to_render = [cut]
            else:
                progress("extraindo_audio", 18)
                media.extract(source, duration, settings, check)
                progress("transcrevendo", 27)
                progress_file = folder / "transcribe.progress"
                last_percent = 27

                def transcribe_check():
                    nonlocal last_percent
                    check()
                    if progress_file.exists():
                        percent = 27 + round(int(progress_file.read_text()) * 0.20)
                        if percent > last_percent:
                            queue.progress(job_id, "transcrevendo", percent)
                            last_percent = percent

                media.run([sys.executable, str(TRANSCRIBER), settings.whisper,
                           str(settings.threads), job.options.idioma], folder, transcribe_check,
                          "Não foi possível transcrever o áudio. Verifique o idioma e o modelo Whisper.", 7200)
                words = json.loads((folder / "words.json").read_text())
                words.sort(key=lambda item: item["inicio"])
                if not words:
                    raise Problem("Não encontramos fala no áudio deste vídeo.")
                clips = selection.select(
                    words, job.options, duration, settings, check, progress,
                    offline=studio_job(job) and not settings.llm_key,
                ) if studio_job(job) else selection.select(words, job.options, duration, settings, check, progress)
                for index, clip in enumerate(clips, 1):
                    clip["id"] = f"corte-{index}"
                to_render = clips
            storage = LocalStorage(settings.data) if studio_job(job) and not settings.studio_s3 else Storage()
            for index, clip in enumerate(to_render):
                check()
                progress("renderizando", 66 + int(30 * index / len(to_render)))
                clip_folder = folder / clip["id"]
                clip_folder.mkdir()
                clip_source = clip_folder / source.name
                clip_source.hardlink_to(source)
                video, cover = media.render(clip_source, words, clip, job.options, edit, settings, check)
                progress("enviando", 66 + int((30 * index + 25) / len(to_render)))
                clip.update(storage.put(job_id, clip["id"], video, cover, check,
                                        lambda key: queue.record(job_id, key)))
                shutil.rmtree(clip_folder)
    except Cancelled:
        if not stop.is_set():
            try:
                clean_objects(queue, settings, job_id, True)
            except Exception:
                LOG.warning("Objetos pendentes serão removidos pela fila de exclusão.")
        return
    except Problem as exc:
        error = str(exc)
    except MemoryError:
        error = "Memória insuficiente para processar este vídeo."
    except OSError as exc:
        error = ("Espaço insuficiente para processar este vídeo." if exc.errno == 28
                 else "Não foi possível acessar os arquivos do processamento.")
    except Exception as exc:
        LOG.error("job=%s falha=%s", job_id, type(exc).__name__)
        error = "O processamento encontrou um erro interno."
    if error:
        LOG.warning("job=%s erro=%s", job_id, error)
        try:
            clean_objects(queue, settings, job_id, True)
        except Exception:
            LOG.warning("Não foi possível limpar os uploads pendentes de %s.", job_id)
    queue.finish(job, error, clips, words if not error else None,
                 duration if not error else None, fingerprint if not error else None)


def deliver(queue, settings, stop):
    while not stop.is_set():
        try:
            row = queue.next_delivery()
            if row is None:
                stop.wait(0.4)
                continue
            body = row["body"].encode()
            signature = hmac.new(settings.secret.encode(), body, hashlib.sha256).hexdigest()
            ok = False
            try:
                with request(row["url"], "POST", body, {
                    "Content-Type": "application/json", "X-Signature": signature,
                    "X-Job-Id": row["job"],
                }, read_timeout=12) as response:
                    ok = 200 <= response.status < 300
            except Exception:
                pass
            LOG.info("job=%s callback tentativa=%s entregue=%s", row["job"], row["attempt"] + 1, ok)
            queue.delivered(row, ok)
        except Exception as exc:
            LOG.error("Falha na fila de webhooks (%s).", type(exc).__name__)
            stop.wait(2)
