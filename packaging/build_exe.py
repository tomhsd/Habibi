"""
Build Habibi.exe for Windows with PyInstaller.

    python packaging/build_exe.py

Output:
    dist/Habibi/Habibi.exe              (the app folder)
    dist/Habibi-Windows-portable.zip    (same folder, zipped)

Habibi discovers its skills (actions/ and plugins/) from .py files at runtime,
so PyInstaller can't see their imports. This script reads those files, collects
every third-party module they import and passes them as hidden imports. The app
files sit next to Habibi.exe (--contents-directory .), so the paths the code
already uses (config/, memory/, dashboard/static/ ...) work unchanged.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
WORK = ROOT / "build_tmp"
LOCAL = {"actions", "core", "memory", "dashboard", "config", "plugins", "ui", "main", "setup"}


def third_party_imports() -> list[str]:
    files = []
    for folder in ("actions", "plugins", "core", "memory", "dashboard"):
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

    sep = os.pathsep
    data = [
        ("actions", "actions"),
        ("plugins", "plugins"),
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
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(WORK),
        "--paths", str(ROOT),
    ]
    for pkg in ("core", "memory", "dashboard", "config", "uvicorn"):
        args += ["--collect-submodules", pkg]
    for pkg in ("playwright", "openwakeword", "sherpa_onnx"):
        if importlib.util.find_spec(pkg) is not None:
            args += ["--collect-all", pkg]
    for src, dest in data:
        if (ROOT / src).exists():
            args += ["--add-data", f"{ROOT / src}{sep}{dest}"]
    hidden = third_party_imports()
    print(f"[build] {len(hidden)} hidden imports from skills/plugins")
    for mod in hidden:
        args += ["--hidden-import", mod]

    pyi.run(args)

    app = DIST / "Habibi"
    for extra in ("LICENSE", "readme.md"):
        if (ROOT / extra).exists():
            shutil.copy2(ROOT / extra, app / extra)
    archive = shutil.make_archive(str(DIST / "Habibi-Windows-portable"), "zip", DIST, "Habibi")
    print(f"[build] done: {app / 'Habibi.exe'}")
    print(f"[build] zip:  {archive}")


if __name__ == "__main__":
    main()
