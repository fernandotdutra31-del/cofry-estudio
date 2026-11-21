import os
from urllib.parse import quote
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import Problem
from .network import validate

ERRORS = (BotoCoreError, ClientError, OSError)


class Storage:
    def __init__(self):
        self.bucket = os.getenv("STORAGE_BUCKET", "")
        self.public = os.getenv("STORAGE_PUBLIC_BASE_URL", "").rstrip("/")
        self.prefix = os.getenv("STORAGE_PREFIX", "cofry").strip("/")
        if self.prefix.split("/")[0] == "studio":
            raise Problem("Use um STORAGE_PREFIX diferente de studio.")
        endpoint = os.getenv("STORAGE_ENDPOINT_URL", "")
        access = os.getenv("STORAGE_ACCESS_KEY_ID", "")
        secret = os.getenv("STORAGE_SECRET_ACCESS_KEY", "")
        if not all((self.bucket, self.public, endpoint, access, secret)):
            raise Problem("Configure o armazenamento S3 antes de gerar os cortes.")
        validate(self.public)
        validate(endpoint)
        self.client = boto3.client(
            "s3", endpoint_url=endpoint, region_name=os.getenv("STORAGE_REGION", "auto"),
            aws_access_key_id=access, aws_secret_access_key=secret,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          connect_timeout=10, read_timeout=45,
                          retries={"total_max_attempts": 3}, request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )

    def put(self, job_id, clip_id, video, cover, check, written):
        urls = {}
        revision = uuid4().hex
        try:
            for field, path, content in (("url_mp4", video, "video/mp4"), ("url_capa", cover, "image/jpeg")):
                check()
                key = f"{self.prefix}/{job_id}/{clip_id}/{revision}/{path.name}".lstrip("/")
                written(key)
                self.client.upload_file(str(path), self.bucket, key, ExtraArgs={
                    "ContentType": content, "CacheControl": "public, max-age=300"
                }, Callback=lambda _: check())
                urls[field] = f"{self.public}/{quote(key, safe='/')}"
        except ERRORS as exc:
            raise Problem("Não foi possível enviar os arquivos para o armazenamento.") from exc
        return urls

    def delete(self, keys):
        for key in keys:
            self.client.delete_object(Bucket=self.bucket, Key=key)

