from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|passphrase|token|api[_-]?key|secret|credential|"
    r"authorization|auth[_-]?(?:sig|signature)|signature|private[_-]?key|"
    r"client[_-]?secret|access[_-]?key)",
    re.IGNORECASE,
)
_KEY_VALUE = re.compile(
    r"(?im)^(\s*[\"']?(?:[-\w.]*?(?:password|passwd|passphrase|token|api[_-]?key|"
    r"secret|credential|authorization|private[_-]?key|client[_-]?secret|"
    r"access[_-]?key)[-\w.]*)[\"']?\s*[:=]\s*)([^\r\n#]+)"
)
_INLINE_KEY_VALUE = re.compile(
    r"(?i)((?:^|[{,]\s*)[\"']?[-\w.]*(?:password|passwd|passphrase|token|"
    r"api[_-]?key|secret|credential|authorization|private[_-]?key|"
    r"client[_-]?secret|access[_-]?key|auth[_-]?(?:sig|signature)|signature)"
    r"[-\w.]*[\"']?\s*:\s*)"
    r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^,}\r\n]+)"
)
_TOKEN_ASSIGNMENT = re.compile(
    r"(?i)(\b[-\w.]*(?:password|passwd|passphrase|token|api[_-]?key|secret|"
    r"credential|authorization|private[_-]?key|client[_-]?secret|"
    r"access[_-]?key|auth[_-]?(?:sig|signature)|signature)[-\w.]*\s*[:=]\s*)"
    r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}&#]+)"
)
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_URL_USERINFO = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]*)?@"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access[_-]?token|token|api[_-]?key|auth(?:sig|signature)?|"
    r"signature|sig|password|passwd|secret|key)=)([^&#\s]+)"
)
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)


def redact_text(value: str) -> str:
    """Redact common secret forms before data can leave the add-on."""
    value = _PRIVATE_KEY.sub(REDACTED, value)
    value = _OPENAI_KEY.sub(REDACTED, value)
    value = _BEARER.sub(r"\1" + REDACTED, value)
    value = _URL_USERINFO.sub(r"\1" + REDACTED + "@", value)
    value = _QUERY_SECRET.sub(lambda match: match.group(1) + REDACTED, value)
    value = _KEY_VALUE.sub(lambda match: match.group(1) + REDACTED, value)
    value = _INLINE_KEY_VALUE.sub(lambda match: match.group(1) + REDACTED, value)
    return _TOKEN_ASSIGNMENT.sub(lambda match: match.group(1) + REDACTED, value)


def redact_data(value: Any, *, key: str = "") -> Any:
    """Recursively redact sensitive mapping values and secret-looking text."""
    if key and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): redact_data(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
