"""Credential storage and redaction.

Secrets live in the OS keychain (macOS Keychain / Windows Credential Locker / Secret Service) via `keyring`.
If no keychain backend is available, they fall back to ~/.evidencedesk/credentials.json with 0600 permissions
(reported as `storage: file` in the settings UI). Secrets are never returned by the API or MCP tools, never
written into exports, and are scrubbed from every stored error message and log line.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading

from .db import data_dir

SERVICE = "evidencedesk"

# Credential schema per source. "secret" fields are kept in the keychain; others are plain settings.
CREDENTIAL_FIELDS: dict[str, list[dict]] = {
    "contact": [{"key": "email", "label": "Contact e-mail (NCBI tool/email, OpenAlex/Crossref polite pool, "
                                         "Unpaywall – required by Unpaywall)", "secret": False}],
    "pubmed": [{"key": "api_key", "label": "NCBI API key (optional; raises limit 3 -> 10 req/s)", "secret": True}],
    "europepmc": [],
    "ctgov": [],
    "openalex": [{"key": "api_key", "label": "OpenAlex API key (free; anonymous use shares a tiny daily budget)",
                  "secret": True}],
    "crossref": [{"key": "plus_token", "label": "Crossref Metadata Plus token (optional)", "secret": True}],
    "unpaywall": [],
    "scopus": [{"key": "api_key", "label": "Elsevier API key (dev.elsevier.com)", "secret": True},
               {"key": "insttoken", "label": "Elsevier institutional token (if off-campus)", "secret": True}],
    "sciencedirect": [{"key": "api_key", "label": "Elsevier API key (dev.elsevier.com)", "secret": True},
                      {"key": "insttoken", "label": "Elsevier institutional token", "secret": True}],
    "embase": [{"key": "api_key", "label": "Elsevier Embase API key (institutional Embase API entitlement)",
                "secret": True},
               {"key": "insttoken", "label": "Elsevier institutional token", "secret": True}],
    "wos": [{"key": "api_key", "label": "Clarivate Web of Science Starter API key", "secret": True}],
}

_lock = threading.RLock()
_known_secrets: set[str] = set()
_cache: dict[str, str | None] = {}


def _file_path():
    return data_dir() / "credentials.json"


def _keyring():
    if os.environ.get("EVIDENCEDESK_NO_KEYRING"):
        return None
    try:
        import keyring
        from keyring.backends import fail
        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def storage_backend() -> str:
    return "keychain" if _keyring() else "file"


def _read_file() -> dict:
    p = _file_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text() or "{}")


def _write_file(d: dict) -> None:
    p = _file_path()
    p.write_text(json.dumps(d, indent=1))
    os.chmod(p, 0o600)


def _settings_path():
    return data_dir() / "settings.json"


def get_setting(source: str, key: str) -> str | None:
    p = _settings_path()
    if not p.exists():
        return None
    return json.loads(p.read_text() or "{}").get(source, {}).get(key)


def set_setting(source: str, key: str, value: str | None) -> None:
    with _lock:
        p = _settings_path()
        d = json.loads(p.read_text() or "{}") if p.exists() else {}
        d.setdefault(source, {})
        if value in (None, ""):
            d[source].pop(key, None)
        else:
            d[source][key] = value
        p.write_text(json.dumps(d, indent=1))


def _is_secret(source: str, key: str) -> bool:
    for f in CREDENTIAL_FIELDS.get(source, []):
        if f["key"] == key:
            return f["secret"]
    return True


def get(source: str, key: str) -> str | None:
    env = os.environ.get(f"EVIDENCEDESK_{source.upper()}_{key.upper()}")
    if env:
        register_secret(env) if _is_secret(source, key) else None
        return env
    if not _is_secret(source, key):
        return get_setting(source, key)
    ck = f"{source}:{key}"
    # Keychain access is serialised and cached per process: concurrent Security-framework calls from worker
    # threads can block indefinitely on macOS.
    with _lock:
        if ck in _cache:
            return _cache[ck]
        kr = _keyring()
        val = None
        if kr:
            try:
                val = kr.get_password(SERVICE, ck)
            except Exception:
                val = None
        if val is None:
            val = _read_file().get(source, {}).get(key)
        _cache[ck] = val
    if val:
        register_secret(val)
    return val


def set(source: str, key: str, value: str | None) -> str:
    if not _is_secret(source, key):
        set_setting(source, key, value)
        return "settings"
    kr = _keyring()
    with _lock:
        _cache.pop(f"{source}:{key}", None)
        if kr:
            try:
                if value:
                    kr.set_password(SERVICE, f"{source}:{key}", value)
                else:
                    try:
                        kr.delete_password(SERVICE, f"{source}:{key}")
                    except Exception:
                        pass
                if value:
                    register_secret(value)
                return "keychain"
            except Exception:
                pass
        d = _read_file()
        d.setdefault(source, {})
        if value:
            d[source][key] = value
            register_secret(value)
        else:
            d[source].pop(key, None)
        _write_file(d)
        return "file"


def status(source: str) -> dict:
    """Configured/not configured per field. Never returns values."""
    out = {}
    for f in CREDENTIAL_FIELDS.get(source, []):
        v = get(source, f["key"])
        out[f["key"]] = {"label": f["label"], "secret": f["secret"], "configured": bool(v),
                         "value": (v if not f["secret"] else None)}
    return out


def register_secret(v: str) -> None:
    if v and len(v) >= 6:
        _known_secrets.add(v)


def load_all_secrets() -> None:
    for src, fields in CREDENTIAL_FIELDS.items():
        for f in fields:
            if f["secret"]:
                get(src, f["key"])


_PARAM_RE = re.compile(r"(?i)((?:api_key|apikey|api-key|insttoken|token|x-els-apikey|x-apikey|access_token)"
                       r"[=:]\s*)([^&\s\"',]+)")


def redact(text):
    """Remove every known secret (and anything that looks like a key parameter) from text."""
    if text is None:
        return None
    s = str(text)
    for sec in sorted(_known_secrets, key=len, reverse=True):
        s = s.replace(sec, "***REDACTED***")
    return _PARAM_RE.sub(lambda m: m.group(1) + "***REDACTED***", s)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # keep args structure intact (uvicorn's access formatter unpacks them); redact each string piece
        record.msg = redact(record.msg) if isinstance(record.msg, str) else record.msg
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True


def install_log_redaction() -> None:
    load_all_secrets()
    f = RedactingFilter()
    for name in ("", "httpx", "uvicorn", "uvicorn.access", "uvicorn.error", "evidencedesk"):
        lg = logging.getLogger(name)
        lg.addFilter(f)
        for h in lg.handlers:
            h.addFilter(f)
