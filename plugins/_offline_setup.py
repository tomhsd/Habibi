"""
One-click setup for offline mode (⚙ PLUGINS → OFFLINE MODE → SET UP OFFLINE VOICE).
Needs the internet once; afterwards everything runs on the PC.

  1. Python packages faster-whisper and piper-tts (skipped in the .exe build,
     which ships them already).
  2. The Whisper speech model (tiny ≈ 75 MB, base ≈ 145 MB, small ≈ 480 MB).
  3. A Piper voice (≈ 60 MB) into models/piper/.
  4. Ollama (installed with winget if missing) and the local model
     (qwen2.5:3b ≈ 1.9 GB).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

PIPER_VOICES = {
    # language → (file stem, Hugging Face path)
    "en": ("en_US-lessac-medium", "en/en_US/lessac/medium"),
    "ar": ("ar_JO-kareem-medium", "ar/ar_JO/kareem/medium"),
    "hi": ("hi_IN-pratham-medium", "hi/hi_IN/pratham/medium"),
}
HF_PIPER = "https://huggingface.co/rhasspy/piper-voices/resolve/main/{path}/{stem}{ext}"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# faster-whisper and piper-tts need PyAV ("av"), which only ships ready-made
# Windows builds for supported Python versions; on newer ones pip tries to
# compile it and fails. Above this version we don't try.
NEWEST_PYTHON = (3, 14)


def python_hint() -> str:
    """What to do when this Python can't install the offline voice."""
    venv = _base_dir() / ".venv312" / "Scripts" / "pythonw.exe"
    if venv.exists():
        return ("Close Habibi and start it with 'Start Habibi (offline-ready).bat' — "
                "that copy uses Python 3.12 and already has the offline voice installed.")
    return ("Install Python 3.12 and run Habibi with it, or use HabibiSetup.exe, "
            "which already includes the offline voice.")


def _pip(pkgs: list[str], log) -> bool:
    if getattr(sys, "frozen", False):
        return True
    missing = [p for p, mod in pkgs if importlib.util.find_spec(mod) is None]
    if not missing:
        return True
    ver = ".".join(map(str, sys.version_info[:2]))
    if sys.version_info[:2] > NEWEST_PYTHON:
        log(f"ERR: Offline voice can't be installed on Python {ver}. {python_hint()}")
        return False
    log(f"SYS: Offline setup: installing {', '.join(missing)}…")
    r = subprocess.run([sys.executable, "-m", "pip", "install", *missing,
                        "--disable-pip-version-check", "--quiet"],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        out = r.stderr or r.stdout or ""
        if "Failed to build" in out and "av" in out:
            log(f"ERR: Offline voice can't be installed on Python {ver} "
                f"(its audio library has no ready-made build for it). {python_hint()}")
        else:
            log(f"ERR: pip failed: {out[-300:]}")
        return False
    importlib.invalidate_caches()
    return True


def _whisper(model: str, log) -> bool:
    log(f"SYS: Offline setup: downloading the Whisper '{model}' speech model…")
    try:
        from faster_whisper import WhisperModel
        WhisperModel(model, device="cpu", compute_type="int8")
        return True
    except Exception as e:
        log(f"ERR: Whisper download failed: {e}")
        return False


def _download(url: str, dest: Path) -> None:
    import requests
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    tmp.replace(dest)


def _piper_voice(language: str, log) -> bool:
    stem, path = PIPER_VOICES.get(language, PIPER_VOICES["en"])
    folder = _base_dir() / "models" / "piper"
    onnx = folder / f"{stem}.onnx"
    if onnx.exists() and Path(str(onnx) + ".json").exists():
        return True
    log(f"SYS: Offline setup: downloading the Piper voice {stem}…")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _download(HF_PIPER.format(path=path, stem=stem, ext=".onnx.json"), Path(str(onnx) + ".json"))
        _download(HF_PIPER.format(path=path, stem=stem, ext=".onnx"), onnx)
        return True
    except Exception as e:
        log(f"ERR: Piper voice download failed: {e} (the Windows voice will be used)")
        return False


def find_ollama() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(local) if local.exists() else None


def _ollama(model: str, log) -> bool:
    exe = find_ollama()
    if exe is None and sys.platform == "win32" and shutil.which("winget"):
        log("SYS: Offline setup: installing Ollama with winget…")
        subprocess.run(["winget", "install", "-e", "--id", "Ollama.Ollama", "--silent",
                        "--accept-package-agreements", "--accept-source-agreements"],
                       capture_output=True, text=True, creationflags=NO_WINDOW, timeout=900)
        exe = find_ollama()
    if exe is None:
        log("ERR: Ollama isn't installed. Get it from https://ollama.com/download, then press setup again.")
        return False
    try:
        from core.llm_client import ensure_ollama_running
        ensure_ollama_running(timeout=20)
    except Exception:
        pass
    log(f"SYS: Offline setup: downloading the local model {model} (about 2 GB, a few minutes)…")
    r = subprocess.run([exe, "pull", model], capture_output=True, text=True,
                       creationflags=NO_WINDOW, timeout=3600)
    if r.returncode != 0:
        log(f"ERR: ollama pull failed: {(r.stderr or r.stdout)[-300:]}")
        return False
    return True


def run_setup(values: dict, log: Callable[[str], None] = print) -> tuple[bool, str]:
    model = str(values.get("model") or "qwen2.5:3b").strip()
    stt = str(values.get("stt_model") or "base")
    lang = str(values.get("language") or "auto")
    results = {}
    results["packages"] = _pip([("faster-whisper", "faster_whisper"), ("piper-tts", "piper")], log)
    results["speech"] = results["packages"] and _whisper(stt, log)
    results["voice"] = results["packages"] and _piper_voice("en" if lang == "auto" else lang, log)
    results["brain"] = _ollama(model, log)
    failed = [k for k, ok in results.items() if not ok]
    if not failed:
        msg = "Offline voice is ready — Habibi can listen, think and talk without internet."
        log(f"SYS: {msg}")
        return True, msg
    if failed == ["voice"]:
        return True, "Ready (using the Windows voice — the Piper voice didn't download)."
    if "packages" in failed:
        return False, "Speech can't be installed here. " + python_hint()
    return False, "Not finished: " + ", ".join(failed) + " — see the log, then try again."
