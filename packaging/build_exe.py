"""
Build Habibi.exe for Windows with PyInstaller.

    python packaging/build_exe.py --edition online
    python packaging/build_exe.py --edition offline

Two separate editions:
    online   Habibi with Gemini Live only. No offline-mode plugins, no Piper voice.
    offline  everything in online, plus offline mode (local listening, thinking
             and speaking when there is no internet).

Output (EDITION = Online or Offline):
    dist/<EDITION>/Habibi/Habibi.exe                (the app folder)
    dist/Habibi-<EDITION>-Windows-portable.zip      (same folder, zipped)

Habibi discovers its skills (actions/ and plugins/) from .py files at runtime,
so PyInstaller can't see their imports. This script reads those files, collects
every third-party module they import and passes them as hidden imports. The app
files sit next to Habibi.exe (--contents-directory .), so the paths the code
already uses (config/, memory/, dashboard/static/ ...) work unchanged.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import shutil
import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
WORK = ROOT / "build_tmp"
LOCAL = {"actions", "core", "memory", "dashboard", "config", "plugins", "ui", "main", "setup"}

# Plugin files that make up offline mode. The online edition leaves them out.
OFFLINE_PLUGINS = ("offline_mode.py", "offline_cache.py", "_offline_engine.py", "_offline_setup.py")
# Packages only offline mode needs (faster-whisper stays: "Hey Habibi" uses it).
OFFLINE_PACKAGES = ("piper", "piper_phonemize")


def edition_plugins(edition: str) -> list[Path]:
    files = sorted((ROOT / "plugins").glob("*.py"))
    if edition == "online":
        files = [f for f in files if f.name not in OFFLINE_PLUGINS]
    return files


def third_party_imports(edition: str = "offline") -> list[str]:
    files = edition_plugins(edition)
    for folder in ("actions", "core", "memory", "dashboard"):
        files += list((ROOT / folder).glob("*.py"))
    files += [ROOT / "main.py", ROOT / "ui.py"]
    found: set[str] = set()
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in LOCAL or top in sys.stdlib_module_names:
                    continue
                if edition == "online" and top in OFFLINE_PACKAGES:
                    continue
                found.add(name)
    usable = []
    for name in sorted(found):
        try:
            if importlib.util.find_spec(name.split(".")[0]) is not None:
                usable.append(name)
        except Exception:
            pass
    return usable


def main() -> None:
    import PyInstaller.__main__ as pyi

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--edition", choices=("online", "offline"),
                    default=os.environ.get("HABIBI_EDITION", "offline"))
    edition = ap.parse_args().edition
    label = edition.capitalize()
    dist = DIST / label
    work = WORK / edition

    # The plugins folder this edition ships with (a copy, so the repo is untouched).
    plug_dir = Path(tempfile.mkdtemp(prefix=f"habibi-{edition}-plugins-")) / "plugins"
    plug_dir.mkdir()
    for f in edition_plugins(edition):
        shutil.copy2(f, plug_dir / f.name)
    print(f"[build] {label} edition: {len(list(plug_dir.glob('*.py')))} plugin files")

    sep = os.pathsep
    data = [
        ("actions", "actions"),
        (str(plug_dir), "plugins"),
        ("dashboard/static", "dashboard/static"),
        ("desktop", "desktop"),
        ("core/prompt.txt", "core"),
        ("config/habibi.ico", "config"),
        ("config/settings.json", "config"),
        ("habibi_description.txt", "."),
        ("config/models", "config/models"),     # voice-lock speaker model (downloaded in CI)
    ]
    args = [
        str(ROOT / "main.py"),
        "--name", "Habibi",
        "--noconfirm", "--clean", "--windowed",
        "--contents-directory", ".",
        "--icon", str(ROOT / "config" / "habibi.ico"),
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(work),
        "--paths", str(ROOT),
    ]
    for pkg in ("core", "memory", "dashboard", "config", "uvicorn", "pynput", "winrt", "pywinauto"):
        args += ["--collect-submodules", pkg]
    collect = ["playwright", "openwakeword", "sherpa_onnx", "imageio_ffmpeg", "rapidocr_onnxruntime",
               "faster_whisper", "ctranslate2"]          # faster-whisper: "Hey Habibi" + offline listening
    if edition == "offline":
        collect += ["piper"]                             # offline speaking
    else:
        for pkg in OFFLINE_PACKAGES:
            args += ["--exclude-module", pkg]
    for pkg in collect:
        if importlib.util.find_spec(pkg) is not None:
            args += ["--collect-all", pkg]
    for src, dest in data:
        if (ROOT / src).exists():
            args += ["--add-data", f"{ROOT / src}{sep}{dest}"]
    for mod in ("ui_media", "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets",
                "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngineCore"):   # built-in media player
        args += ["--hidden-import", mod]
    hidden = third_party_imports(edition)
    print(f"[build] {len(hidden)} hidden imports from skills/plugins")
    for mod in hidden:
        args += ["--hidden-import", mod]

    pyi.run(args)

    app = dist / "Habibi"
    for extra in ("LICENSE", "readme.md"):
        if (ROOT / extra).exists():
            shutil.copy2(ROOT / extra, app / extra)
    (app / "EDITION.txt").write_text(f"Habibi {label}\n", encoding="utf-8")
    shutil.rmtree(plug_dir.parent, ignore_errors=True)
    archive = shutil.make_archive(str(DIST / f"Habibi-{label}-Windows-portable"), "zip", dist, "Habibi")
    print(f"[build] done: {app / 'Habibi.exe'}")
    print(f"[build] zip:  {archive}")


if __name__ == "__main__":
    main()
