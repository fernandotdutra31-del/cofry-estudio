import json
import logging
import math
import subprocess
import threading
import time
from collections import deque
from copy import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib3.exceptions import HTTPError

from .config import Cancelled, Problem
from .network import normalize, request, validate, youtube_proxy, youtube_url

LOG = logging.getLogger(__name__)
MEDIA_SLOT = threading.Lock()
YOUTUBE = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
DEMUXERS = "mov,matroska,webm,avi,mpegts,mpeg,flv,ogg,wav,mp3"
INPUT = ["-protocol_whitelist", "file", "-format_whitelist", DEMUXERS]


def run(args, folder, check, message, timeout=1800):
    with (folder / "stderr.log").open("w+b") as stderr, (folder / "stdout.log").open("w+b") as stdout:
        start = time.monotonic()
        with subprocess.Popen(args, cwd=folder, stdin=subprocess.DEVNULL, stderr=stderr, stdout=stdout) as proc:
            try:
                while proc.poll() is None:
                    check()
                    if time.monotonic() - start > timeout:
                        raise Problem("A etapa excedeu o tempo limite.")
                    time.sleep(0.1)
                if proc.returncode:
                    stderr.seek(0)
                    raw = stderr.read(12000).decode(errors="replace").lower()
                    if proc.returncode == -9 or "cannot allocate memory" in raw or "out of memory" in raw:
                        raise Problem("Memória insuficiente para processar este vídeo.")
                    if args[0] == "yt-dlp":
                        reasons = (
                            (("sign in to confirm", "not a bot"), "verificação de acesso exigida pelo YouTube"),
                            (("403 forbidden", "http error 403"), "acesso recusado"),
                            (("requested format is not available",), "formato compatível indisponível"),
                            (("timed out", "unable to connect", "connection refused"), "conexão indisponível"),
                        )
                        reason = next((label for phrases, label in reasons if any(p in raw for p in phrases)),
                                      "falha de extração")
                        LOG.warning("yt-dlp retornou %s (%s)", proc.returncode, reason)
                    else:
                        LOG.warning("%s retornou %s", args[0], proc.returncode)
                    raise Problem(message)
                stdout.seek(0)
                return stdout.read(2_000_000)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


def download(url, folder, settings, check, youtube_timeout=900):
    target = folder / "source.media"
    message = "Não foi possível baixar este link. Envie o arquivo."
    try:
        if validate(url).hostname in YOUTUBE:
            if not settings.youtube:
                raise Problem("O download do YouTube está desativado.")
            def limited():
                check()
                if sum(file.stat().st_size for file in folder.glob("yt.*") if file.is_file()) > settings.max_bytes:
                    raise Problem("O vídeo excede o tamanho máximo permitido.")

            with youtube_proxy() as proxy:
                run(["yt-dlp", "--ignore-config", "--no-playlist", "--no-progress",
                     "--proxy", proxy, "--js-runtimes", "node", "--socket-timeout", "20", "--downloader", "native",
                     "--max-filesize", str(settings.max_bytes),
                     "--match-filter", f"duration <= {settings.max_seconds} & !is_live",
                     "-f", "best[height<=1080][vcodec!=none][acodec!=none]",
                     "-o", "yt.%(ext)s", "--", youtube_url(url)], folder, limited, message, youtube_timeout)
            files = [file for file in folder.glob("yt.*") if file.is_file()]
            if len(files) != 1:
                raise Problem(message)
            files[0].replace(target)
            if target.stat().st_size > settings.max_bytes:
                raise Problem("O vídeo excede o tamanho máximo permitido.")
        else:
            deadline = time.monotonic() + 900
            with request(normalize(url), redirects=6, headers={"Accept-Encoding": "identity"}) as response:
                if response.status != 200 or "text/" in response.headers.get("Content-Type", "").lower():
                    raise Problem(message)
                if int(response.headers.get("Content-Length", "0")) > settings.max_bytes:
                    raise Problem("O vídeo excede o tamanho máximo permitido.")
                size = 0
                with target.open("wb") as out:
                    for chunk in response.stream(256 * 1024, decode_content=False):
                        check()
                        size += len(chunk)
                        if size > settings.max_bytes:
                            raise Problem("O vídeo excede o tamanho máximo permitido.")
                        if time.monotonic() > deadline:
                            raise Problem(message)
                        out.write(chunk)
    except Problem:
        raise
    except (OSError, ValueError, HTTPError) as exc:
        raise Problem(message) from exc
    return target


def check_youtube(settings, stop):
    trial = copy(settings)
    trial.youtube = True
    trial.max_bytes = min(settings.max_bytes, 8 * 1024**2)
    trial.max_seconds = min(settings.max_seconds, 60)

    def check():
        if stop.is_set():
            raise Cancelled

    try:
        with TemporaryDirectory(prefix="task-youtube-check-", dir=settings.data) as directory:
            asset = download(youtube_url(settings.youtube_check_url), Path(directory), trial, check,
                             youtube_timeout=55)
            if not asset.stat().st_size:
                raise Problem("A amostra do YouTube estava vazia.")
    except Cancelled:
        return
    except Exception as exc:
        settings.youtube_status = "indisponivel"
        LOG.warning("Teste do YouTube: indisponível (%s).", type(exc).__name__)
    else:
        settings.youtube = True
        settings.youtube_status = "disponivel"
        LOG.info("Teste do YouTube: download da amostra confirmado.")


def ff(folder, check, threads, *args):
    return run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-filter_threads", str(threads), "-filter_complex_threads", str(threads), *map(str, args)],
               folder, check, "Não foi possível processar o vídeo com FFmpeg.")


def probe(source, limits, check):
    raw = run(["ffprobe", "-v", "error", *INPUT, "-show_entries",
               "format=duration:stream=codec_type,width,height:stream_disposition=attached_pic", "-of", "json", str(source)],
              source.parent, check, "O arquivo não é um vídeo válido.", 60)
    try:
        data = json.loads(raw)
        duration = float(data["format"]["duration"])
        streams = data["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video"
                     and not stream.get("disposition", {}).get("attached_pic"))
        if not any(stream["codec_type"] == "audio" for stream in streams):
            raise ValueError
    except (KeyError, ValueError, StopIteration):
        raise Problem("O arquivo precisa conter vídeo e áudio válidos.") from None
    if not 120 <= duration <= limits.max_seconds:
        raise Problem(f"O vídeo deve ter entre 120 e {limits.max_seconds} segundos.")
    if video["width"] * video["height"] > 4096 * 2160:
        raise Problem("A resolução do vídeo excede o limite de processamento.")
    return duration


def extract(source, duration, settings, check):
    folder = source.parent
    ff(folder, check, settings.threads, "-threads", settings.threads, *INPUT, "-i", source,
       "-map", "0:a:0", "-t", duration, "-ac", 1, "-ar", 16000, "-c:a", "pcm_s16le", "speech.wav")


def make_timeline(words, start, end):
    keep = []
    cursor = start
    last = start
    for word in words:
        if word["fim"] <= start or word["inicio"] >= end:
            continue
        begin = max(start, word["inicio"])
        if begin - last > 0.4:
            left, right = last + 0.08, begin - 0.08
            if left > cursor:
                keep.append((cursor, left))
            cursor = right
        last = max(last, min(end, word["fim"]))
    stop = min(end, last + 0.08) if end - last > 0.4 and last > start else end
    if stop > cursor:
        keep.append((cursor, stop))
    return keep or [(start, end)]


def segment_length(start, end):
    return max(1, math.ceil((end - start) * 30 - 1e-7)) / 30


def remap(keep, begin, end):
    cursor = 0
    for a, b in keep:
        left, right = max(a, begin), min(b, end)
        if right > left:
            yield cursor + left - a, cursor + right - a
        cursor += segment_length(a, b)


def ass_time(seconds):
    cs = round(max(0, seconds) * 100)
    whole, fraction = divmod(cs, 100)
    minutes, second = divmod(whole, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours}:{minute:02}:{second:02}.{fraction:02}"


def safe_ass(value):
    return (value.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
            .replace("\r", " ").replace("\n", " "))


STYLES = {
    "branco": ("&H00FFFFFF", 1, 4, -1, 0, 63),
    "amarelo": ("&H0000FFFF", 1, 5, -1, 0, 65),
    "neon_verde": ("&H0030FF60", 1, 5, -1, 0, 65),
    "caixa": ("&H00FFFFFF", 3, 9, -1, 0, 62),
    "minimalista": ("&H00FFFFFF", 1, 2, 0, 0, 51),
    "editorial": ("&H00E2F8FF", 1, 3, 0, -1, 70),
    "karaoke": ("&H0000EFFF", 1, 4, -1, 0, 66),
    "contorno": ("&H00FFFFFF", 1, 9, -1, 0, 68),
    "italico_caixa": ("&H00FFFFFF", 3, 8, -1, -1, 64),
    "laranja": ("&H002E93FF", 1, 5, -1, 0, 66),
}


def subtitles(path, size, style, position, events, hook="", headline="", cover=""):
    width, height = size
    alignment = {"superior": 8, "central": 5, "inferior": 2}[position]
    color, border, outline, bold, italic, font = STYLES[style]
    font_size = round(font * min(width / 1080, height / 1080))
    margin = round(height * 0.12)
    def row(name, color, pixels, align, margin, border=1, outline=4, bold=-1, italic=0):
        return (f"Style: {name},DejaVu Sans,{pixels},{color},&H00FFFFFF,&H00101010,"
                f"&H60101010,{bold},{italic},0,0,100,100,0,0,{border},{outline},1,{align},65,65,{margin}\n")
    header = (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV\n"
        + row("Caption", color, font_size, alignment, margin, border, outline, bold, italic)
        + row("Hook", "&H00FFFFFF", round(font_size * 1.52), 5, margin, 3, 11)
        + row("Headline", color, round(font_size * 0.9), 8 if alignment != 8 else 2, height // 20,
              border, outline, bold, italic)
        + row("Cover", "&H00FFFFFF", round(font_size * 1.46), 5, margin, 3, 12)
        + "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    def line(a, b, text, name="Caption", layer=0):
        return f"Dialogue: {layer},{ass_time(a)},{ass_time(b)},{name},,0,0,0,,{text}\n"
    with path.open("w", encoding="utf-8") as out:
        out.write(header)
        if cover:
            out.write(line(0, 2, safe_ass(cover), "Cover"))
            return
        if headline:
            out.write(line(0, max(3, max((b for _, b, _ in events), default=0)), safe_ass(headline), "Headline", 1))
        if hook:
            out.write(line(0, 3, safe_ass(hook), "Hook", 2))
        for a, b, text in events:
            if hook and position == "central":
                a = max(a, 3)
            if ass_time(b) != ass_time(a) and b > a:
                out.write(line(a, b, text))


def caption_events(words, keep, edit, style):
    if edit is not None:
        return [(a, b, safe_ass(block.texto)) for block in edit.blocos
                for a, b in remap(keep, block.inicio, block.fim)]
    relevant = [word for word in words if any(
        word["inicio"] < b and word["fim"] > a for a, b in keep)]
    events = []
    for index, word in enumerate(relevant):
        text = safe_ass(word["texto"])
        if style == "karaoke":
            group_start = (index // 4) * 4
            group = relevant[group_start:group_start + 4]
            text = " ".join(
                ("{\\1c&H0000EFFF&}" if group_start + i <= index else "{\\1c&H00FFFFFF&}")
                + safe_ass(item["texto"]) for i, item in enumerate(group)
            )
        events.extend((a, b, text) for a, b in remap(keep, word["inicio"], word["fim"]))
    return events


def tracking(source, start, end, crop_w, crop_h, width, height, timeline, folder, check):
    import cv2
    cv2.setNumThreads(1)
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    # Decode through FFmpeg with the same protocol/demuxer restrictions as rendering.
    small_w, small_h = 320, max(2, round(320 * height / width / 2) * 2)
    ff(folder, check, 1, "-ss", start, *INPUT, "-i", source, "-t", end - start,
       "-map", "0:V:0", "-an", "-vf", f"fps=4,scale={small_w}:{small_h},format=gray", "-pix_fmt", "gray", "-f", "rawvideo", "tracking.raw")
    import numpy as np
    smooth_x, smooth_y = deque(maxlen=5), deque(maxlen=5)
    previous = None
    commands = []
    with (folder / "tracking.raw").open("rb") as frames:
        for index in range(int((end - start) * 4) + 1):
            check()
            frame = frames.read(small_w * small_h)
            if len(frame) < small_w * small_h:
                break
            gray = np.frombuffer(frame, np.uint8).reshape((small_h, small_w))
            faces = detector.detectMultiScale(gray, 1.15, 4, minSize=(22, 22))
            if len(faces):
                def score(face):
                    x, y, w, h = map(int, face)
                    if previous is None:
                        return float(w * h)
                    mouth = np.s_[y + h * 2 // 3:y + h, x + w // 4:x + w * 3 // 4]
                    forehead = np.s_[y:y + h // 3, x + w // 4:x + w * 3 // 4]
                    change = cv2.absdiff(gray, previous)
                    return max(0.0, change[mouth].mean() - change[forehead].mean()) + w / 20
                x, y, w, h = max(faces, key=score)
                smooth_x.append((x + w / 2) * width / small_w)
                smooth_y.append((y + h / 2) * height / small_h)
            else:
                smooth_x.append(width / 2)
                smooth_y.append(height / 2)
            previous = gray
            t = start + index / 4
            mapped = list(remap(timeline, t, t + 0.001))
            if mapped:
                x = max(0, min(width - crop_w, round(sum(smooth_x) / len(smooth_x) - crop_w / 2)))
                y = max(0, min(height - crop_h, round(sum(smooth_y) / len(smooth_y) - crop_h / 2)))
                commands.append(f"{mapped[0][0]:.5f} crop@face x {x}, crop@face y {y};")
    if commands:
        (folder / "follow.cmd").write_text("\n".join(commands))
    return bool(commands)


def render(source, words, candidate, options, edit, settings, check):
    folder = source.parent
    start, end = candidate["inicio"], candidate["fim"]
    timeline = make_timeline(words, start, end)
    dims = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}
    width, height = dims[options.formato]
    style = edit.legenda.estilo if edit and edit.legenda is not None else (edit.estilo_legenda if edit and edit.estilo_legenda else options.estilo_legenda)
    hook = options.gancho if not edit or edit.gancho is None else edit.gancho
    headline = options.manchete if not edit or edit.manchete is None else edit.manchete
    subtitles(folder / "captions.ass", (width, height), style, options.posicao_legenda,
              caption_events(words, timeline, edit.legenda if edit else None, style),
              candidate["gancho"] if hook is True else (hook or ""),
              candidate["titulo"] if headline is True else (headline or ""))
    subtitles(folder / "cover.ass", (width, height), style, options.posicao_legenda,
              [], cover=candidate["titulo"])
    # Keep only the processed interval files in this directory. PCM avoids AAC priming
    # at each join. A final pass encodes the combined audio and burns continuous ASS.
    parts = []
    entries = []
    for index, (a, b) in enumerate(timeline):
        name = f"part-{index}.mkv"
        duration = segment_length(a, b)
        ff(folder, check, settings.threads, "-threads", settings.threads, "-ss", a, *INPUT, "-i", source,
           "-t", f"{duration:.9f}", "-map", "0:V:0", "-map", "0:a:0", "-vf",
           "scale=w='min(iw,1920)':h='min(ih,1920)':force_original_aspect_ratio=decrease:"
           "force_divisible_by=2,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=0.1",
           "-af", "apad=pad_dur=0.1", "-c:v", "libx264", "-preset", "ultrafast", "-crf", 17, "-pix_fmt", "yuv420p",
           "-threads", settings.threads, "-c:a", "pcm_s16le", "-ar", 48000, name)
        parts.append(name)
        entries.append(f"file '{name}'\nduration {duration:.9f}\n")
    (folder / "join.txt").write_text("".join(entries))
    fit = f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    if options.enquadramento == "inteiro":
        filters = f"{fit},pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    elif options.enquadramento == "desfocado":
        filters = (f"split[bg][fg];[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
                   f"crop={width}:{height},boxblur=24:2[b];[fg]{fit}[f];[b][f]overlay=(W-w)/2:(H-h)/2")
    else:
        filters = (f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2,"
                   f"crop={width}:{height}")
        if options.seguir_rosto:
            info = json.loads(run(["ffprobe", "-v", "error", *INPUT, "-select_streams", "V:0",
                                   "-show_entries", "stream=width,height", "-of", "json", str(source)],
                                  folder, check, "Não foi possível ler as dimensões do vídeo.", 30))
            video = info["streams"][0]
            sw, sh = video["width"], video["height"]
            scale = max(width / sw, height / sh)
            scaled_w, scaled_h = round(sw * scale / 2) * 2, round(sh * scale / 2) * 2
            if tracking(source, start, end, width, height, scaled_w, scaled_h, timeline, folder, check):
                filters = (f"scale={scaled_w}:{scaled_h},sendcmd=f=follow.cmd,"
                           f"crop@face={width}:{height}")
    graph = f"[0:v]{filters},fps=30,setsar=1,ass=captions.ass[v]"
    ff(folder, check, settings.threads, "-threads", settings.threads, "-f", "concat", "-safe", 1,
       "-protocol_whitelist", "file", "-i", "join.txt", "-filter_complex", graph, "-map", "[v]",
       "-map", "0:a:0", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000", "-c:a", "aac",
       "-b:a", "160k", "-c:v", "libx264", "-preset", "veryfast", "-crf", 21, "-pix_fmt", "yuv420p",
       "-threads", settings.threads, "-movflags", "+faststart", "clip.mp4")
    ff(folder, check, settings.threads, "-ss", min(0.25, (timeline[0][1] - timeline[0][0]) / 3),
       *INPUT, "-i", parts[0], "-filter_complex",
       f"[0:v]{filters},setsar=1,ass=cover.ass[v]", "-map", "[v]",
       "-frames:v", 1, "-update", 1, "cover.jpg")
    return folder / "clip.mp4", folder / "cover.jpg"
