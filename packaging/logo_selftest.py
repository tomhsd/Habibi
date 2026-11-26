"""Custom logo selftest: the user's picture becomes Habibi's logo everywhere.

  • PNG / JPG / BMP / non-square / transparent-border pictures -> .ico with all
    Windows sizes + 256px PNG, centred, never stretched; bad files refused
  • the real MainWindow (offscreen): ⚙ buttons, window + app icon, header
    logo swapped, CREATE DESKTOP SHORTCUT uses the custom icon, reset works
  • Windows: real .lnk shortcuts (desktop, Start menu, taskbar pin) get the
    icon, through pywin32 and through the no-pywin32 PowerShell path; other
    apps' shortcuts are left alone; reset puts Habibi.exe's icon back
  • Linux: .desktop files get Icon=
Saves logo_selftest.png (the header with a custom logo)."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

FAILS: list[str] = []
try:   # Windows CI console is cp1252; test names have symbols
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def check(name: str, ok: bool, detail="") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS.append(name)


import PIL.Image  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="logo_selftest_"))
PICS = TMP / "pics"
PICS.mkdir()

# ── test pictures ─────────────────────────────────────────────────────────────
wide = PIL.Image.new("RGBA", (400, 200), (0, 0, 0, 0))
for x in range(40, 360):
    for y in range(60, 140):
        wide.putpixel((x, y), (230, 40, 40, 255))
wide.save(PICS / "wide logo.png")                     # transparent border + not square
PIL.Image.new("RGB", (300, 500), (20, 120, 220)).save(PICS / "tall.jpg", quality=90)
PIL.Image.new("RGB", (64, 64), (250, 200, 0)).save(PICS / "small.bmp")
(PICS / "notapicture.png").write_text("hello")

# ══════════════════════════════════════════════════════════════════════════════
#  1. picture -> icon
# ══════════════════════════════════════════════════════════════════════════════
sys.path.insert(0, str(ROOT / "plugins"))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("plugins.custom_logo", ROOT / "plugins" / "custom_logo.py")
CL = importlib.util.module_from_spec(spec)
sys.modules["plugins.custom_logo"] = CL
spec.loader.exec_module(CL)
L = CL.L

OUT = TMP / "logo"
for name in ("wide logo.png", "tall.jpg", "small.bmp"):
    ico, png = L.make_icon(PICS / name, OUT, stem=f"t_{Path(name).stem.replace(' ', '_')}")
    sizes = L.ico_sizes(ico)
    with PIL.Image.open(png) as im:
        im = im.convert("RGBA")
        corner = im.getpixel((0, 0))[3]
        centre = im.getpixel((128, 128))
    check(f"{name}: .ico has every Windows size", all(s in sizes for s in (16, 24, 32, 48, 64, 256)), str(sizes))
    check(f"{name}: PNG is 256x256", im.size == (256, 256))
    check(f"{name}: picture centred (middle filled)", centre[3] == 255, str(centre))
    if name != "small.bmp":
        check(f"{name}: not stretched (padding is transparent)", corner == 0, f"corner alpha {corner}")

# wide logo: transparent border trimmed, so the red bar fills the full width
with PIL.Image.open(OUT / "t_wide_logo.png") as im:
    row = [im.convert("RGBA").getpixel((x, 128))[3] for x in (2, 253)]
check("transparent border trimmed (logo fills the icon)", min(row) == 255, str(row))

try:
    L.make_icon(PICS / "notapicture.png", OUT)
    check("not-a-picture refused", False)
except ValueError as e:
    check("not-a-picture refused with a plain message", "not a picture" in str(e), str(e))
try:
    L.make_icon(PICS / "missing.png", OUT)
    check("missing file refused", False)
except ValueError:
    check("missing file refused", True)

# ── shortcut matching ─────────────────────────────────────────────────────────
exe = str(Path(sys.executable).resolve())
check("matches Habibi.exe shortcut", L.is_habibi_shortcut(exe, "", exe, ""))
check("ignores other programs", not L.is_habibi_shortcut(r"C:\Windows\notepad.exe", "", exe, ""))
main_py = str(ROOT / "main.py")
check("matches pythonw main.py shortcut (from source)",
      L.is_habibi_shortcut(str(Path(sys.executable).parent / "pythonw.exe"), f'"{main_py}"',
                           exe, main_py) if sys.platform == "win32" else
      L.is_habibi_shortcut("/usr/bin/python3", main_py, exe, main_py))
check("ignores python running something else",
      not L.is_habibi_shortcut(str(Path(sys.executable)), '"C:\\other\\app.py"', exe, main_py))

# ══════════════════════════════════════════════════════════════════════════════
#  2. the real window
# ══════════════════════════════════════════════════════════════════════════════
STORE: dict = {}
CL._cfg = lambda: dict(STORE)
CL._cfg_save = lambda v: STORE.update(v)
CL.logo_dir = lambda: TMP / "app_logo"

import ui  # noqa: E402
from PyQt6.QtWidgets import QApplication, QPushButton  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
win = ui.MainWindow("")
win.show()
app.processEvents()


class Player:
    def __init__(self, w):
        self._win = w
        self.log: list[str] = []
        self.current_file = None

    def write_log(self, t):
        self.log.append(t)

    def run_in_ui(self, fn, timeout=15):
        return fn()


player = Player(win)
SYNCS: list = []
real_sync_bg = CL._sync_bg
CL._sync_bg = lambda p, announce=False: (announce or CL.current_logo()) and SYNCS.append(announce)  # shortcuts tested below

CL.setup_ui(player)
btns = [b.text() for b in win._quick_drawer.findChildren(QPushButton)]
check("⚙ drawer has CHANGE LOGO", any("CHANGE LOGO" in t for t in btns))
check("USE HABIBI LOGO hidden while the normal logo is on", win._custom_logo_btns[1].isHidden())
check("normal start never touches shortcuts", SYNCS == [])
CL.setup_ui(player)   # plugin reload: no duplicate buttons
app.processEvents()
reset_btn = win._custom_logo_btns[1]
check("reload does not duplicate the buttons",
      sum(1 for b in win._quick_drawer.findChildren(QPushButton) if "CHANGE LOGO" in b.text()
          and b.parent() is not None and win._quick_drawer.layout().indexOf(b) >= 0) == 1)

reply = CL.run({"action": "set", "path": str(PICS / "tall.jpg")}, player=player)
app.processEvents()
check("voice: set logo replies", "new logo" in reply, reply)
logo = CL.current_logo()
check("logo saved", logo is not None and logo[0].exists())
icon = app.windowIcon()
check("app icon is the custom logo", not icon.isNull() and icon.availableSizes(), str(icon.availableSizes()))
check("window icon set", not win.windowIcon().isNull())
check("desktop-shortcut button will use the custom icon", win.app_icon_path == str(logo[0]))
marks = CL._logo_marks(win)
twin = getattr(marks[0], "_custom_logo_twin", None) if marks else None
check("header logo swapped for the picture", bool(marks) and marks[0].isHidden()
      and twin is not None and twin.isVisible())
check("USE HABIBI LOGO shown", not reset_btn.isHidden())
check("shortcut update started", SYNCS == [True])
try:
    hdr = marks[0].parentWidget()
    hdr.grab().save(str(ROOT / "logo_selftest.png"))
except Exception as e:
    print("screenshot failed:", e)

first = logo[0]
CL.run({"action": "set", "path": str(PICS / "wide logo.png")}, player=player)
logo2 = CL.current_logo()
check("second change gets a new file name (beats Windows icon cache)", logo2[0] != first)
check("old logo file removed", not first.exists())

player.current_file = str(PICS / "small.bmp")
reply = CL.run({"action": "set"}, player=player)
check("voice: 'use this picture' takes the dropped picture", "small.bmp" in reply, reply)
player.current_file = None
check("status names the picture", "small.bmp" in CL.run({"action": "status"}))

# CREATE DESKTOP SHORTCUT uses the custom icon (Windows: real .lnk)
if sys.platform == "win32":
    dsk = TMP / "Desktop"
    dsk.mkdir()
    win._get_desktop_dir = lambda: dsk
    win._create_desktop_shortcut()
    lnk = dsk / "Habibi.lnk"
    try:
        from win32com.client import Dispatch
        loc = Dispatch("WScript.Shell").CreateShortCut(str(lnk)).IconLocation
    except Exception as e:
        loc = f"error {e}"
    check("CREATE DESKTOP SHORTCUT uses the custom logo",
          os.path.normcase(str(CL.current_logo()[0])) in os.path.normcase(loc), loc)

CL.run({"action": "reset"}, player=player)
app.processEvents()
check("reset: header logo back", not marks[0].isHidden() and twin.isHidden())
check("reset: desktop shortcut icon back to Habibi's", win.app_icon_path is None)
check("reset: setting cleared", CL.current_logo() is None)
check("reset: button hidden again", reset_btn.isHidden())
CL._sync_bg = real_sync_bg

# ══════════════════════════════════════════════════════════════════════════════
#  3. shortcuts
# ══════════════════════════════════════════════════════════════════════════════
if sys.platform == "win32":
    from win32com.client import Dispatch
    sh = Dispatch("WScript.Shell")
    fake_exe = str(Path(sys.executable).resolve())          # stands in for Habibi.exe
    home = TMP / "win"
    dirs = {"desk": home / "Desktop",
            "start": home / "Start Menu" / "Programs" / "Habibi Online",
            "pin": home / "User Pinned" / "TaskBar"}
    for d in dirs.values():
        d.mkdir(parents=True)

    def make(path, target, args=""):
        sc = sh.CreateShortCut(str(path))
        sc.TargetPath = target
        sc.Arguments = args
        sc.Save()

    make(dirs["desk"] / "Habibi.lnk", fake_exe)
    make(dirs["start"] / "Habibi Online.lnk", fake_exe)
    make(dirs["pin"] / "Habibi Offline.lnk", fake_exe)
    notepad = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "notepad.exe")
    make(dirs["desk"] / "Notepad.lnk", notepad)
    scan = [(dirs["desk"], False), (home / "Start Menu" / "Programs", True), (dirs["pin"], True)]
    ico, _png = L.make_icon(PICS / "tall.jpg", TMP / "lnk_logo", stem="lnktest")

    def icon_of(p):
        return sh.CreateShortCut(str(p)).IconLocation

    for use_com in (True, False):
        label = "pywin32" if use_com else "PowerShell (no pywin32)"
        res = L.sync_shortcuts(str(ico), dirs=scan, exe=fake_exe, script="", use_com=use_com)
        check(f"{label}: found desktop, Start menu and pinned shortcut", len(res["found"]) == 3, str(res))
        check(f"{label}: all three changed", len(res["changed"]) == 3, str(res["failed"]))
        ok = all(os.path.normcase(str(ico)) in os.path.normcase(icon_of(p)) for p in
                 (dirs["desk"] / "Habibi.lnk", dirs["start"] / "Habibi Online.lnk",
                  dirs["pin"] / "Habibi Offline.lnk"))
        check(f"{label}: shortcuts show the new logo", ok)
        check(f"{label}: other apps' shortcuts untouched",
              str(ico).lower() not in icon_of(dirs["desk"] / "Notepad.lnk").lower(),
              icon_of(dirs["desk"] / "Notepad.lnk"))
        again = L.sync_shortcuts(str(ico), dirs=scan, exe=fake_exe, script="", use_com=use_com)
        check(f"{label}: second run changes nothing", again["changed"] == [], str(again))
        back = L.sync_shortcuts(f"{fake_exe},0", dirs=scan, exe=fake_exe, script="", use_com=use_com)
        check(f"{label}: reset puts Habibi.exe's icon back", len(back["changed"]) == 3
              and fake_exe.lower() in icon_of(dirs["pin"] / "Habibi Offline.lnk").lower(),
              icon_of(dirs["pin"] / "Habibi Offline.lnk"))
    L.refresh_shell([])
    check("refresh_shell runs", True)
elif sys.platform.startswith("linux"):
    d = TMP / "apps"
    d.mkdir()
    (d / "Habibi.desktop").write_text(f"[Desktop Entry]\nName=Habibi\nExec=python3 {ROOT / 'main.py'}\n"
                                      "Icon=/old.png\nType=Application\n")
    (d / "Other.desktop").write_text("[Desktop Entry]\nName=Other\nExec=other\nIcon=/x.png\n")
    ico, png = L.make_icon(PICS / "tall.jpg", TMP / "lnk_logo", stem="lnktest")
    res = L.sync_shortcuts(str(ico), dirs=[(d, False)])
    txt = (d / "Habibi.desktop").read_text()
    check("Linux: Habibi .desktop gets the logo", f"Icon={png}" in txt and "/old.png" not in txt, txt)
    check("Linux: other .desktop untouched", "Icon=/x.png" in (d / "Other.desktop").read_text())

win.close()
shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL PASS")
