import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from urllib3.exceptions import HTTPError

from .config import Problem
from .media import MEDIA_SLOT
from .network import read_limited, request

MODEL = "@cf/black-forest-labs/flux-1-schnell"
MODEL_IDS = {"flux-cloudflare", "animacao-cloudflare"}
DIMENSIONS = {"16:9": (960, 540), "9:16": (540, 960), "1:1": (768, 768)}


@dataclass(frozen=True)
class Credentials:
    account_id: str
    token: str

    @classmethod
    def configured(cls):
        account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        token = os.getenv("CLOUDFLARE_AI_TOKEN", "").strip()
        if (os.getenv("CLOUDFLARE_WORKERS_FREE", "").lower() != "true"
                or not re.fullmatch(r"[a-fA-F0-9]{32}", account)
                or not re.fullmatch(r"[A-Za-z0-9_-]{15,256}", token)):
            return None
        return cls(account, token)


def generate_image(credentials, prompt):
    url = f"https://api.cloudflare.com/client/v4/accounts/{credentials.account_id}/ai/run/{MODEL}"
    try:
        with request(url, method="POST", body=json.dumps({"prompt": prompt, "steps": 4}).encode(),
                     headers={"Authorization": f"Bearer {credentials.token}", "Content-Type": "application/json"},
                     read_timeout=75) as response:
            if response.status in {401, 403}:
                raise Problem("Confira a permissão Workers AI do token na Cloudflare.")
            if response.status == 429:
                raise Problem("A Cloudflare atingiu o limite de uso. Tente novamente mais tarde.")
            if response.status != 200:
                raise Problem("A Cloudflare recusou o pedido. Consulte a conta e a franquia do Workers AI.")
            body = json.loads(read_limited(response, 12 * 1024**2))
            if body.get("success") is not True:
                raise Problem("A Cloudflare não concluiu a geração.")
            image = base64.b64decode(body["result"]["image"], validate=True)
            if not image.startswith(b"\xff\xd8\xff"):
                raise ValueError
            return image
    except (OSError, HTTPError):
        raise Problem("A Cloudflare não respondeu a tempo. Tente novamente mais tarde.") from None
    except (ValueError, KeyError, AttributeError, TypeError):
        raise Problem("A Cloudflare retornou um arquivo de imagem inválido.") from None


def save_result(image, model_id, ratio, duration, destination):
    width, height = DIMENSIONS[ratio]
    shape = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
    output = "resultado.mp4" if model_id == "animacao-cloudflare" else "resultado.jpg"
    destination.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination) as directory:
        scratch = Path(directory)
        (scratch / "source.jpg").write_bytes(image)
        args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-threads", "1", "-filter_threads", "1",
                "-f", "image2", "-i", "source.jpg"]
        if model_id == "animacao-cloudflare":
            frames = duration * 24
            motion = (f"zoompan=z='min(1.15,1+0.15*on/{max(1, frames-1)})':"
                      f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d={frames}:s={width}x{height}:fps=24")
            args += ["-vf", f"{shape},{motion},format=yuv420p", "-frames:v", str(frames),
                     "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-movflags", "+faststart"]
        else:
            args += ["-vf", shape, "-frames:v", "1", "-update", "1", "-q:v", "3"]
        try:
            with MEDIA_SLOT:
                subprocess.run([*args, output], cwd=scratch, timeout=240, check=True, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            raise Problem("Não foi possível salvar o resultado da geração.") from None
        (scratch / output).replace(destination / output)
