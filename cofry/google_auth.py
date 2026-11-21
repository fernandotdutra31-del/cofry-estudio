import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from urllib3.exceptions import HTTPError

from .config import Problem
from .network import read_limited, request, validate


AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CALLBACK_PATH = "/studio/auth/google/callback"
STATE_COOKIE = "cofry_google_state"
STATE_TTL = 600
STATE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


class GoogleDenied(Exception):
    pass


class GoogleRetry(Exception):
    pass


def owner_email():
    value = os.getenv("OWNER_EMAIL", "").strip().casefold()
    return value if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) else None


@dataclass(frozen=True)
class GoogleOAuth:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    owner: str

    @classmethod
    def configured(cls):
        client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
        redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "").strip()
        owner = owner_email()
        if not (client_id and client_secret and redirect_uri and owner):
            return None
        try:
            url = validate(redirect_uri)
        except Problem:
            return None
        if url.path != CALLBACK_PATH or url.query or url.fragment:
            return None
        return cls(client_id, client_secret, redirect_uri, owner)

    def authorization_url(self, state, verifier):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return AUTHORIZATION_URL + "?" + urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        })

    def check_owner(self, code, verifier):
        token = google_json(TOKEN_URL, method="POST", body=urlencode({
            "code": code,
            "code_verifier": verifier,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})

        access = token.get("access_token")
        if not isinstance(access, str) or not re.fullmatch(r"[!-~]{1,8192}", access):
            raise GoogleRetry
        if str(token.get("token_type", "")).casefold() != "bearer":
            raise GoogleRetry

        profile = google_json(USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
        email = profile.get("email")
        subject = profile.get("sub")
        if (
            profile.get("email_verified") is not True
            or not isinstance(subject, str)
            or not subject
            or not isinstance(email, str)
            or email.casefold() != self.owner
        ):
            raise GoogleDenied


def google_json(url, *, method="GET", body=None, headers):
    try:
        with request(
            url, method=method, body=body,
            headers={**headers, "Accept": "application/json", "Accept-Encoding": "identity"},
            read_timeout=15,
        ) as response:
            if response.status != 200:
                raise GoogleRetry
            data = json.loads(read_limited(response, 32_768))
    except (OSError, HTTPError, ValueError, Problem) as exc:
        raise GoogleRetry from exc
    if not isinstance(data, dict):
        raise GoogleRetry
    return data


def state_mac(key, message):
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def create_state(key):
    state = secrets.token_urlsafe(32)
    expires = int(time.time()) + STATE_TTL
    digest = state_mac(key, f"google-state:{expires}:{state}")
    return state, f"{expires}.{state}.{digest}"


def verify_state(key, state, cookie):
    if not STATE_PATTERN.fullmatch(state):
        return False
    try:
        expires, stored, digest = cookie.split(".")
        return (
            int(expires) > time.time()
            and hmac.compare_digest(stored.encode(), state.encode())
            and hmac.compare_digest(
                digest.encode(), state_mac(key, f"google-state:{expires}:{stored}").encode()
            )
        )
    except ValueError:
        return False


def code_verifier(key, state):
    return state_mac(key, f"google-pkce:{state}")
