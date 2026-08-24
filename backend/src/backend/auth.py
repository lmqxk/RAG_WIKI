"""认证基础：PBKDF2 密码哈希与 HS256 JWT，不依赖外部加密包。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

PASSWORD_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), _b64decode(raw_salt), int(raw_iterations)
        )
        return hmac.compare_digest(digest, _b64decode(raw_digest))
    except (TypeError, ValueError):
        return False


def create_access_token(payload: dict[str, Any], secret: str, expires_seconds: int) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {**payload, "exp": int(time.time()) + expires_seconds}
    encoded_header = _b64encode(json.dumps(header, separators=(",", ":")).encode())
    encoded_claims = _b64encode(json.dumps(claims, separators=(",", ":")).encode())
    signature = hmac.new(
        secret.encode(), f"{encoded_header}.{encoded_claims}".encode(), hashlib.sha256
    )
    return f"{encoded_header}.{encoded_claims}.{_b64encode(signature.digest())}"


def decode_access_token(token: str, secret: str) -> dict[str, Any] | None:
    try:
        encoded_header, encoded_claims, encoded_signature = token.split(".")
        header = json.loads(_b64decode(encoded_header))
        claims = json.loads(_b64decode(encoded_claims))
        expected = hmac.new(
            secret.encode(), f"{encoded_header}.{encoded_claims}".encode(), hashlib.sha256
        ).digest()
        if header != {"alg": "HS256", "typ": "JWT"}:
            return None
        if not hmac.compare_digest(expected, _b64decode(encoded_signature)):
            return None
        if not isinstance(claims, dict) or int(claims.get("exp", 0)) <= time.time():
            return None
        return claims
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
