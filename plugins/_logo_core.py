"""
Helpers for the custom_logo plugin (no Qt here, so they are easy to test).

  make_icon()        any picture -> square multi-size .ico + 256px .png
  sync_shortcuts()   point Habibi's shortcuts (desktop, Start menu, taskbar
                     pins) at an icon; returns what changed
  set_uninstall_icon() the icon shown in Windows "Installed apps"
  refresh_shell()    ask Windows to redraw shortcut icons

Shortcuts are matched by what they START (Habibi.exe, or pythonw main.py when
run from source), never by their name, so "Habibi", "Habibi Online" and
"Habibi Offline" are all found and other apps' shortcuts are never touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".ico", ".tif", ".tiff")
MAX_BYTES = 40 * 1024 * 1024


# ══════════════════════════════════════════════════════════════════════════════
#  Picture -> icon
# ══════════════════════════════════════════════════════════════════════════════

def make_icon(src, out_dir, stem: str | None = None) -> tuple[Path, Path]:
    """Turn a picture into <stem>.ico (all Windows sizes) and <stem>.png
    (256x256). The picture is centred on a transparent square, so nothing is
    cut off and nothing is stretched. A new stem every time on purpose:
    Windows caches icons by file path, so a new path shows up at once.
    Raises ValueError with a plain message when the file can't be used."""
    import PIL.Image
    import PIL.ImageOps

    src = Path(src)
    if not src.is_file():
        raise ValueError(f"I can't find the picture {src}.")
    if src.stat().st_size > MAX_BYTES:
        raise ValueError("That picture is too big (over 40 MB).")
    try:
        img = PIL.Image.open(src)
        if getattr(img, "n_frames", 1) > 1:
            img.seek(0)                         # animated GIF/WebP: first frame
        img = PIL.ImageOps.exif_transpose(img)  # phone photos: keep them upright
        img.load()
    except Exception:
        raise ValueError(f"{src.name} is not a picture I can read. Use PNG, JPG, ICO, BMP, GIF or WebP.")
    img = img.convert("RGBA")
    if img.width < 1 or img.height < 1:
        raise ValueError("That picture is empty.")

    img = trim_transparent(img)
    side = max(img.width, img.height)
    square = PIL.Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    big = square.resize((256, 256), PIL.Image.LANCZOS)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or f"logo_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"
    ico, png = out_dir / f"{stem}.ico", out_dir / f"{stem}.png"
    big.save(png, format="PNG")
    big.save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    return ico, png


def trim_transparent(img):
    """Cut away fully transparent borders (logos are often exported with lots
    of empty space, which makes them tiny in the taskbar)."""
    try:
        box = img.getchannel("A").getbbox()
        if box and box != (0, 0, img.width, img.height):
            return img.crop(box)
    except Exception:
        pass
    return img


def ico_sizes(path) -> list[int]:
    import PIL.Image
    with PIL.Image.open(path) as im:
        return sorted(w for (w, h) in im.info.get("sizes", set()))


# ══════════════════════════════════════════════════════════════════════════════
#  Which shortcuts are Habibi's
# ══════════════════════════════════════════════════════════════════════════════

def app_target() -> tuple[str, str]:
    """(program, script) a Habibi shortcut starts. Packaged: (Habibi.exe, "").
    From source: (python/pythonw, main.py)."""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve()), ""
    return str(Path(sys.executable).resolve()), str(Path(__file__).resolve().parents[1] / "main.py")


def _norm(p: str) -> str:
    p = os.path.expandvars((p or "").strip().strip('"'))
    try:
        p = str(Path(p).resolve())
    except Exception:
        pass
    return os.path.normcase(os.path.normpath(p)) if p else ""


def is_habibi_shortcut(target: str, args: str, exe: str, script: str) -> bool:
    t = _norm(target)
    if not t:
        return False
    if not script:                                  # packaged Habibi.exe
        return t == _norm(exe)
    # from source: any python of this install running our main.py
    name = os.path.basename(t)
    if not name.startswith("python"):
        return False
    a = os.path.normcase(os.path.expandvars(args or "").replace('"', "").replace("/", os.sep))
    return _norm(script) in a


def windows_shortcut_dirs(desktop: Path | None = None) -> list[tuple[Path, bool]]:
    """(folder, look in subfolders) for every place Windows keeps a shortcut
    the user sees: desktop, Start menu, taskbar pins, start pins (Win 10)."""
    env = os.environ.get
    appdata = Path(env("APPDATA") or Path.home() / "AppData" / "Roaming")
    progdata = Path(env("PROGRAMDATA") or r"C:\ProgramData")
    public = Path(env("PUBLIC") or r"C:\Users\Public")
    ql = appdata / "Microsoft" / "Internet Explorer" / "Quick Launch"
    dirs = [
        (desktop or Path.home() / "Desktop", False),
        (public / "Desktop", False),
        (appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs", True),
        (progdata / "Microsoft" / "Windows" / "Start Menu" / "Programs", True),
        (ql / "User Pinned" / "TaskBar", True),
        (ql / "User Pinned" / "StartMenu", True),
        (ql, False),
    ]
    seen, out = set(), []
    for d, rec in dirs:
        k = _norm(str(d))
        if k not in seen and d.is_dir():
            seen.add(k)
            out.append((d, rec))
    return out


def _lnk_files(dirs) -> list[Path]:
    out = []
    for d, rec in dirs:
        try:
            out += list(d.rglob("*.lnk") if rec else d.glob("*.lnk"))
        except Exception:
            pass
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Update shortcuts
# ══════════════════════════════════════════════════════════════════════════════

def sync_shortcuts(icon_loc: str, dirs=None, exe: str | None = None,
                   script: str | None = None, use_com: bool = True) -> dict:
    """Give every Habibi shortcut the icon `icon_loc` ("path.ico" or
    "Habibi.exe,0"). Returns {"found": [...], "changed": [...], "failed": [...]}.
    Never raises."""
    res = {"found": [], "changed": [], "failed": []}
    if sys.platform == "win32":
        if exe is None:
            exe, script = app_target()
        dirs = windows_shortcut_dirs() if dirs is None else dirs
        files = _lnk_files(dirs)
        if not files:
            return res
        done = False
        if use_com:
            try:
                _sync_com(files, icon_loc, exe, script or "", res)
                done = True
            except ImportError:
                pass
            except Exception as e:
                res["failed"].append(f"COM: {e}")
        if not done:
            _sync_ps(files, icon_loc, exe, script or "", res)
    else:
        _sync_linux(icon_loc, dirs, res)
    return res


def _icon_matches(current: str, wanted: str) -> bool:
    cur = (current or "").strip()
    if not cur or cur.startswith(","):
        cur = ""
    path, _, idx = cur.rpartition(",") if "," in cur else (cur, "", "0")
    wpath, _, widx = wanted.rpartition(",") if "," in wanted else (wanted, "", "0")
    return _norm(path) == _norm(wpath) and (idx or "0").strip() == (widx or "0").strip()


def _sync_com(files, icon_loc, exe, script, res) -> None:
    from win32com.client import Dispatch  # type: ignore   (ImportError -> PowerShell path)
    sh = Dispatch("WScript.Shell")
    for f in files:
        try:
            sc = sh.CreateShortCut(str(f))
            if not is_habibi_shortcut(sc.TargetPath, sc.Arguments, exe, script):
                continue
            res["found"].append(str(f))
            if _icon_matches(sc.IconLocation, icon_loc):
                continue
            sc.IconLocation = icon_loc
            sc.Save()
            res["changed"].append(str(f))
        except Exception as e:
            res["failed"].append(f"{f}: {e}")


# Fallback without pywin32: PowerShell + the same WScript.Shell COM object.
# (VBScript is being removed from Windows, so it isn't used.) CREATE_NO_WINDOW
# keeps it invisible. Paths travel through UTF-16 files so any name works.
_PS_READ = r'''
param($inp, $out)
$ws = New-Object -ComObject WScript.Shell
$lines = Get-Content -LiteralPath $inp -Encoding Unicode
$res = foreach ($p in $lines) {
  if (-not $p) { continue }
  try { $sc = $ws.CreateShortcut($p)
        "L`t$p`t$($sc.TargetPath)`t$($sc.Arguments)`t$($sc.IconLocation)" }
  catch { "E`t$p`t$($_.Exception.Message)" }
}
Set-Content -LiteralPath $out -Value $res -Encoding Unicode
'''

_PS_SET = r'''
param($inp, $out)
$ws = New-Object -ComObject WScript.Shell
$lines = @(Get-Content -LiteralPath $inp -Encoding Unicode)
$icon = $lines[0]
$res = foreach ($p in ($lines | Select-Object -Skip 1)) {
  if (-not $p) { continue }
  try { $sc = $ws.CreateShortcut($p); $sc.IconLocation = $icon; $sc.Save(); "OK`t$p" }
  catch { "E`t$p`t$($_.Exception.Message)" }
}
Set-Content -LiteralPath $out -Value $res -Encoding Unicode
'''


def _run_ps(code: str, lines: list[str]) -> list[str]:
    tmp = Path(tempfile.mkdtemp(prefix="habibi_logo_"))
    try:
        ps1, inp, out = tmp / "s.ps1", tmp / "in.txt", tmp / "out.txt"
        ps1.write_text(code, encoding="utf-8-sig")
        inp.write_text("\r\n".join(lines) + "\r\n", encoding="utf-16")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", str(ps1), str(inp), str(out)],
                           capture_output=True, text=True, timeout=90, creationflags=flags)
        if not out.exists():
            raise RuntimeError((r.stderr or r.stdout or "no output").strip()[:300])
        return out.read_text(encoding="utf-16").splitlines()
    finally:
        for p in tmp.glob("*"):
            try:
                p.unlink()
            except Exception:
                pass
        try:
            tmp.rmdir()
        except Exception:
            pass


def _sync_ps(files, icon_loc, exe, script, res) -> None:
    todo = []
    try:
        listing = _run_ps(_PS_READ, [str(f) for f in files])
    except Exception as e:
        res["failed"].append(f"PowerShell: {e}")
        return
    for line in listing:
        parts = line.split("\t")
        if parts[0] == "L" and len(parts) >= 5:
            _, p, target, args, cur = parts[:5]
            if is_habibi_shortcut(target, args, exe, script):
                res["found"].append(p)
                if not _icon_matches(cur, icon_loc):
                    todo.append(p)
    if not todo:
        return
    try:
        done = _run_ps(_PS_SET, [icon_loc] + todo)
    except Exception as e:
        res["failed"].append(f"PowerShell: {e}")
        return
    for line in done:
        parts = line.split("\t")
        if parts[0] == "OK":
            res["changed"].append(parts[1])
        elif parts[0] == "E":
            res["failed"].append(" ".join(parts[1:]))


def _sync_linux(icon_loc, dirs, res) -> None:
    """Linux: .desktop files on the desktop and in the app menu."""
    if sys.platform == "darwin":
        return
    icon = icon_loc.rpartition(",")[0] if icon_loc.endswith(",0") else icon_loc
    if icon.lower().endswith(".ico") and Path(icon).with_suffix(".png").exists():
        icon = str(Path(icon).with_suffix(".png"))
    main_py = str(Path(__file__).resolve().parents[1] / "main.py")
    if dirs is None:
        dirs = [(Path.home() / "Desktop", False),
                (Path.home() / ".local" / "share" / "applications", False)]
    for d, _rec in dirs:
        try:
            files = list(Path(d).glob("*.desktop"))
        except Exception:
            continue
        for f in files:
            try:
                txt = f.read_text(encoding="utf-8")
                if main_py not in txt and not (getattr(sys, "frozen", False) and sys.executable in txt):
                    continue
                res["found"].append(str(f))
                lines = [ln for ln in txt.splitlines() if not ln.startswith("Icon=")]
                new = "\n".join(lines).rstrip("\n") + f"\nIcon={icon}\n"
                if new != txt:
                    f.write_text(new, encoding="utf-8")
                    res["changed"].append(str(f))
            except Exception as e:
                res["failed"].append(f"{f}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  Windows extras
# ══════════════════════════════════════════════════════════════════════════════

def set_uninstall_icon(icon: str) -> bool:
    """Packaged Habibi: the icon in Settings > Apps > Installed apps.
    Finds Habibi's own uninstall entry by its install folder."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    try:
        import winreg
        app_dir = _norm(str(Path(sys.executable).parent))
        base = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, name, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
                        loc, _ = winreg.QueryValueEx(k, "InstallLocation")
                        if _norm(loc) != app_dir:
                            continue
                        winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, icon)
                        return True
                except OSError:
                    continue
    except Exception:
        pass
    return False


def refresh_shell(paths=()) -> None:
    """Tell Explorer the icons changed so the desktop, Start menu and taskbar
    redraw them without a restart."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        SHCNE_UPDATEITEM, SHCNE_ASSOCCHANGED = 0x00002000, 0x08000000
        SHCNF_IDLIST, SHCNF_PATHW, SHCNF_FLUSH = 0x0000, 0x0005, 0x1000
        sh = ctypes.windll.shell32
        for p in paths:
            sh.SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW | SHCNF_FLUSH, ctypes.c_wchar_p(str(p)), None)
        sh.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST | SHCNF_FLUSH, None, None)
    except Exception:
        pass
    try:   # rebuilds the icon cache (Windows 10/11); a GUI tool, no window
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        exe = Path(sysroot) / "System32" / "ie4uinit.exe"
        if exe.exists():
            subprocess.Popen([str(exe), "-show"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
