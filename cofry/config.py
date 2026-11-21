import os
from pathlib import Path


class Problem(Exception):
    pass


class Cancelled(Exception):
    pass


class Settings:
    def __init__(self):
        self.data = Path(os.getenv("DATA_DIR", "./data")).resolve()
        self.max_bytes = int(os.getenv("MAX_VIDEO_MB", "500")) * 1024**2
        self.max_seconds = int(os.getenv("MAX_VIDEO_SECONDS", "3600"))
        self.whisper = os.getenv("WHISPER_MODEL", "small")
        self.threads = int(os.getenv("PROCESSING_THREADS", "2"))
        self.studio_s3 = os.getenv("STUDIO_USE_S3", "false").lower() == "true"
        self.youtube = os.getenv("ENABLE_URL_DOWNLOAD", "false").lower() == "true"
        self.api_key = os.getenv("PROCESSING_API_KEY", "")
        self.studio_key = os.getenv("STUDIO_ACCESS_KEY") or self.api_key
        self.studio_cookie_secure = os.getenv("STUDIO_COOKIE_SECURE", "false").lower() == "true"
        self.secret = os.getenv("WEBHOOK_SECRET", "")
        self.provider = os.getenv("LLM_PROVIDER", "openai").lower()
        models = {"openai": "gpt-4.1-mini", "anthropic": "claude-sonnet-4-6", "openrouter": "openai/gpt-4.1-mini"}
        self.model = os.getenv("LLM_MODEL", models.get(self.provider, ""))
        self.llm_key = os.getenv("LLM_API_KEY", "")
        if min(self.threads, self.max_bytes) < 1 or self.max_seconds < 120:
            raise RuntimeError("Revise os limites de processamento.")
