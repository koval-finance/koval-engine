"""Authentication helpers shared by sandbox exchange adapters."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping

SECRET_KEYS = {
    "api_key",
    "api_secret",
    "signature",
    "x-mbx-apikey",
    "x-txc-apikey",
    "x-txc-payload",
    "x-txc-signature",
    "authorization",
}

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:"
    r"api[_-]?(?:key|secret)|"
    r"signature|"
    r"x-mbx-apikey|"
    r"x-txc-(?:apikey|payload|signature)|"
    r"authorization|"
    r"password|"
    r"token"
    r")\b\s*[:=]\s*[^&\s,;]+"
)
_SECRET_KEY_RE = re.compile(
    r"(?i)\b(?:"
    r"api[_-]?(?:key|secret)|"
    r"signature|"
    r"x-mbx-apikey|"
    r"x-txc-(?:apikey|payload|signature)|"
    r"authorization|"
    r"password|"
    r"token"
    r")\b"
)


def redact_secret(value: str) -> str:
    del value
    return "<redacted>"


def redact_mapping(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: redact_secret(str(item))
            if str(key).lower() in SECRET_KEYS
            else redact_mapping(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_mapping(item) for item in value)
    return value


def safe_exception_message(exc: BaseException) -> str:
    """Return useful exception text without credential names or assigned values."""
    message = _SECRET_ASSIGNMENT_RE.sub("<credential-redacted>", str(exc))
    return _SECRET_KEY_RE.sub("credential", message)


def hmac_sha256(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def hmac_sha512(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha512).hexdigest()
