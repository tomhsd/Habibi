"""
core/secure.py — keep Habibi's private data encrypted on disk.

On Windows this uses DPAPI (the same protection Windows uses for saved Wi-Fi
and browser passwords): data is locked to YOUR Windows account on THIS PC.
Copying the files elsewhere (backup, USB, another user) only gives scrambled
bytes. On macOS/Linux the functions pass data through unchanged.

What is protected:
  • secret values in config/api_keys.json — any key named *api_key*, *token*,
    *password*, *secret* (also inside plugin_config) — stored as "dpapi:..."
  • memory/long_term.json — the whole file (what Habibi remembers about you)

Readers call reveal(value) / read_protected_text(path); old plain files keep
working and are encrypted the next time Habibi saves them (or at start-up via
migrate_config()).
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

PREFIX = "dpapi:"
FILE_MAGIC = "HABIBI-DPAPI-1\n"
_ENTROPY = b"Habibi secure storage v1"
_SECRET_HINTS = ("api_key", "apikey", "token", "password", "secret", "client_secret")


def available() -> bool:
    return sys.platform == "win32"


# ── DPAPI (Windows) ───────────────────────────────────────────────────────────

def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _mk(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    src, _keep1 = _mk(data)
    ent, _keep2 = _mk(_ENTROPY)
    out = _Blob()
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN
    if encrypt:
        ok = crypt32.CryptProtectData(ctypes.byref(src), ctypes.c_wchar_p("Habibi"),
                                      ctypes.byref(ent), None, None, flags, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(src), None,
                                        ctypes.byref(ent), None, None, flags, ctypes.byref(out))
    if not ok:
        raise OSError("Windows could not " + ("encrypt" if encrypt else "decrypt") +
                      " the data (was it created by another Windows user or PC?)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


# ── single values ─────────────────────────────────────────────────────────────

def protect(text: str) -> str:
    """Encrypt one secret string → 'dpapi:…'. No-op off Windows or if already protected."""
    if not isinstance(text, str) or not text or text.startswith(PREFIX) or not available():
        return text
    try:
        return PREFIX + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")
    except Exception:
        return text


def reveal(value):
    """Decrypt a value written by protect(). Plain values are returned unchanged.
    An undecryptable value returns '' so Habibi asks for the key again."""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    try:
        return _dpapi(base64.b64decode(value[len(PREFIX):]), False).decode("utf-8")
    except Exception:
        return ""


def _is_secret_key(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in _SECRET_HINTS) or n == "key" or n.endswith("_key")


def protect_secrets(obj):
    """Return a copy of a config dict with every secret-looking value encrypted."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and _is_secret_key(str(k)):
                out[k] = protect(v)
            else:
                out[k] = protect_secrets(v)
        return out
    if isinstance(obj, list):
        return [protect_secrets(x) for x in obj]
    return obj


def reveal_secrets(obj):
    """Return a copy of a config dict with every 'dpapi:' value decrypted."""
    if isinstance(obj, dict):
        return {k: reveal_secrets(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [reveal_secrets(x) for x in obj]
    return reveal(obj)


def migrate_config(path) -> bool:
    """Encrypt any plain-text secrets left in api_keys.json. Returns True if changed."""
    p = Path(path)
    if not available() or not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    new = protect_secrets(data)
    if new != data:
        p.write_text(json.dumps(new, indent=4), encoding="utf-8")
        return True
    return False


# ── whole files (memory) ──────────────────────────────────────────────────────

def read_protected_text(path) -> str:
    """Read a file that may be encrypted with write_protected_text()."""
    raw = Path(path).read_text(encoding="utf-8")
    if not raw.startswith(FILE_MAGIC):
        return raw
    return _dpapi(base64.b64decode(raw[len(FILE_MAGIC):].strip()), False).decode("utf-8")


def write_protected_text(path, text: str) -> None:
    """Write text encrypted (Windows) or as-is (other systems)."""
    p = Path(path)
    if available():
        try:
            blob = base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")
            p.write_text(FILE_MAGIC + blob + "\n", encoding="utf-8")
            return
        except Exception:
            pass
    p.write_text(text, encoding="utf-8")
