"""
Custom logo (plugin): make Habibi wear the user's own picture.

Pick any picture (PNG, JPG, ICO, BMP, GIF, WebP) with ⚙ → 🖼 CHANGE LOGO, or
say "change your logo to this picture" (a picture in the drop zone, or a file
path), and it becomes Habibi's logo everywhere:

  • the window / title-bar icon and the taskbar button
  • the logo in Habibi's header
  • desktop and Start-menu shortcuts, and pinned taskbar / Start icons
  • the icon in Windows Settings > Apps (installed Habibi)
  • shortcuts made later with ⚙ → CREATE DESKTOP SHORTCUT

The picture is centred on a transparent square (never cut or stretched) and
saved as a multi-size .ico in config/logo/. Each change gets a new file name,
because Windows caches icons by path. Habibi re-checks its shortcuts when it
starts and every 10 minutes, so a reinstall/update or a fresh pin picks the
logo up again. ↺ HABIBI LOGO (or "use your normal logo") puts everything back.

Works the same in Habibi Online and Habibi Offline: it finds shortcuts by the
program they start, not by their name.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

PLUGIN = {
    "name": "custom_logo",
    "description": (
        "Change Habibi's own logo / app icon to the user's picture, everywhere: window, taskbar, "
        "pinned icons, desktop and Start-menu shortcuts and the header logo. Use when the user says "
        "'change your logo', 'use this picture as your icon', 'set Habibi's logo'. "
        "action=set with path (a picture file; leave path empty to use the picture in the drop zone), "
        "action=pick to open a file picker, action=reset to go back to Habibi's normal logo, "
        "action=status. Not for the window colour (that is the colour setting)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "set, pick, reset or status"},
            "path": {"type": "STRING", "description": "Picture file for action=set (optional)"},
        },
    },
}

NS = "custom_logo"
RESYNC_MS = 10 * 60 * 1000


def _sibling(stem: str):
    """plugins/<stem>.py by path (plugins/ ships as plain files in the .exe)."""
    import importlib.util
    name = f"plugins.{stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{stem}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


L = _sibling("_logo_core")


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def logo_dir() -> Path:
    return _base_dir() / "config" / "logo"


def default_icon_loc() -> str:
    """What shortcuts show when no custom logo is set."""
    if getattr(sys, "frozen", False):
        return f"{Path(sys.executable).resolve()},0"
    ico = _base_dir() / "config" / "habibi.ico"
    return str(ico) if ico.exists() else ""


# ── saved choice ─────────────────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        return get_plugin_config(NS)
    except Exception:
        return {}


def _cfg_save(values: dict) -> None:
    try:
        from memory.config_manager import save_plugin_config
        save_plugin_config(NS, values)
    except Exception as e:
        print(f"[Logo] could not save setting: {e}")


def current_logo() -> tuple[Path, Path] | None:
    """(ico, png) of the user's logo, or None for Habibi's own."""
    stem = str(_cfg().get("file") or "").strip()
    if not stem or "/" in stem or "\\" in stem:
        return None
    ico, png = logo_dir() / f"{stem}.ico", logo_dir() / f"{stem}.png"
    return (ico, png) if ico.exists() and png.exists() else None


def _cleanup(keep: str | None) -> None:
    """Remove older logo files (the shortcuts no longer point at them)."""
    d = logo_dir()
    if not d.is_dir():
        return
    for f in d.iterdir():
        if f.suffix.lower() in (".ico", ".png") and f.stem != keep:
            try:
                f.unlink()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  Qt side: window icon, header logo, ⚙ buttons
# ══════════════════════════════════════════════════════════════════════════════

def _image_mark_class():
    from PyQt6.QtCore import QRectF, Qt
    from PyQt6.QtGui import QPainter, QPixmap
    from PyQt6.QtWidgets import QWidget

    class ImageMark(QWidget):
        """The header logo when the user picked their own picture."""

        def __init__(self, png: str, size: int, parent=None):
            super().__init__(parent)
            self.setObjectName("CustomLogoMark")
            self.setFixedSize(size, size)
            self.set_image(png)

        def set_image(self, png: str) -> None:
            self._px = QPixmap(png)
            self.setToolTip("Habibi's logo — change it in ⚙ → CHANGE LOGO")
            self.update()

        def paintEvent(self, _):
            p = QPainter(self)
            if not p.isActive() or self._px.isNull():
                return
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(QRectF(0, 0, self.width(), self.height()), self._px,
                         QRectF(0, 0, self._px.width(), self._px.height()))
            p.end()

    return ImageMark


def _logo_marks(win) -> list:
    from PyQt6.QtWidgets import QWidget
    return [w for w in win.findChildren(QWidget) if type(w).__name__ == "LogoMark"]


def apply_in_app(win, logo: tuple[Path, Path] | None) -> None:
    """Window/taskbar icon, every Habibi window and the header logo."""
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication

    if logo:
        icon = QIcon(str(logo[0]))
        icon.addFile(str(logo[1]))
        win.app_icon_path = str(logo[0])
    else:
        default = _base_dir() / "config" / "habibi.ico"
        icon = QIcon() if getattr(sys, "frozen", False) or not default.exists() else QIcon(str(default))
        win.app_icon_path = None
    app = QApplication.instance()
    if app is not None:
        app.setWindowIcon(icon)
    win.setWindowIcon(icon)

    ImageMark = _image_mark_class()
    for mark in _logo_marks(win):
        holder = mark.parentWidget()
        custom = getattr(mark, "_custom_logo_twin", None)
        if logo:
            if custom is None:
                custom = ImageMark(str(logo[1]), mark.width(), holder)
                mark._custom_logo_twin = custom
                lay = holder.layout() if holder is not None else None
                if lay is not None:
                    i = lay.indexOf(mark)
                    lay.insertWidget(i if i >= 0 else 0, custom)
            else:
                custom.set_image(str(logo[1]))
            mark.hide()
            custom.show()
        else:
            if custom is not None:
                custom.hide()
            mark.show()


def _add_buttons(player) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QPushButton

    win = player._win
    drawer = getattr(win, "_quick_drawer", None)
    lay = drawer.layout() if drawer is not None else None
    if lay is None:
        return
    old = getattr(win, "_custom_logo_btns", None)   # plugin reloaded: replace ours
    if old:
        for b in old:
            lay.removeWidget(b)
            b.deleteLater()
    anchor = None
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if isinstance(w, QPushButton) and "DESKTOP SHORTCUT" in w.text():
            anchor = w
            break
    style = anchor.styleSheet() if anchor else ""
    font = anchor.font() if anchor else None
    pos = (lay.indexOf(anchor) + 1) if anchor else lay.count()

    def mk(text, tip, fn):
        b = QPushButton(text)
        b.setFixedHeight(26)
        if font is not None:
            b.setFont(font)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet(style)
        b.setToolTip(tip)
        b.clicked.connect(fn)
        return b

    change = mk("🖼  CHANGE LOGO", "Use your own picture as Habibi's logo: window, taskbar, "
                "pinned icons and shortcuts", lambda: _report(player, pick_and_set(player)))
    reset = mk("↺  USE HABIBI LOGO", "Put Habibi's normal logo back everywhere",
               lambda: _report(player, reset_logo(player)))
    lay.insertWidget(pos, change)
    lay.insertWidget(pos + 1, reset)
    reset.setVisible(current_logo() is not None)
    win._custom_logo_btns = (change, reset)
    try:
        win._position_quick_drawer()
    except Exception:
        pass


def _refresh_buttons(player) -> None:
    btns = getattr(getattr(player, "_win", None), "_custom_logo_btns", None)
    if btns:
        btns[1].setVisible(current_logo() is not None)
        try:
            player._win._position_quick_drawer()
        except Exception:
            pass


def _report(player, msg: str) -> None:
    try:
        player.write_log(f"SYS: {msg}")
    except Exception:
        print(msg)


# ══════════════════════════════════════════════════════════════════════════════
#  Shortcuts (background thread: reading .lnk files can take a moment)
# ══════════════════════════════════════════════════════════════════════════════

_sync_lock = threading.Lock()


def sync_now(player=None, announce: bool = False) -> dict:
    """Bring every Habibi shortcut in line with the current logo."""
    with _sync_lock:
        logo = current_logo()
        loc = str(logo[0]) if logo else default_icon_loc()
        res = {"found": [], "changed": [], "failed": []}
        if loc:
            desktop = None
            try:
                win = getattr(player, "_win", None)
                if win is not None:
                    desktop = win._get_desktop_dir()
            except Exception:
                pass
            dirs = L.windows_shortcut_dirs(desktop) if sys.platform == "win32" else None
            res = L.sync_shortcuts(loc, dirs=dirs)
            L.set_uninstall_icon(str(logo[0]) if logo else loc.rpartition(",")[0] or loc)
            if res["changed"]:
                L.refresh_shell(res["changed"])
        if announce and player is not None:
            n, f = len(res["changed"]), len(res["found"])
            if sys.platform == "win32":
                msg = (f"Updated {n} shortcut{'s' if n != 1 else ''}" if n else
                       ("Shortcuts already up to date" if f else "No Habibi shortcuts found yet"))
                _report(player, f"{msg} (desktop, Start menu, taskbar pins).")
            if res["failed"]:
                _report(player, f"Logo: {len(res['failed'])} shortcut(s) could not be changed "
                                "(they may need admin rights).")
        return res


def _sync_bg(player, announce: bool = False) -> None:
    if not current_logo() and not announce:
        return        # nothing custom: never touch shortcuts on a normal start
    threading.Thread(target=sync_now, args=(player, announce), daemon=True,
                     name="custom-logo-sync").start()


# ══════════════════════════════════════════════════════════════════════════════
#  Change / reset
# ══════════════════════════════════════════════════════════════════════════════

def _via_qt(src: Path) -> Path | None:
    """Pictures Pillow can't read (e.g. SVG): let Qt draw them into a PNG."""
    try:
        from PyQt6.QtGui import QImage
        img = QImage(str(src))
        if img.isNull():
            return None
        tmp = logo_dir() / "_import.png"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        if img.width() < 256 and src.suffix.lower() == ".svg":
            img = img.scaled(512, 512)
        return tmp if img.save(str(tmp), "PNG") else None
    except Exception:
        return None


def set_logo(player, src) -> str:
    """Make `src` Habibi's logo. Call on the Qt thread. Returns a spoken reply."""
    src = Path(str(src).strip().strip('"')).expanduser()
    try:
        try:
            ico, png = L.make_icon(src, logo_dir())
        except ValueError:
            alt = _via_qt(src) if src.is_file() else None
            if alt is None:
                raise
            try:
                ico, png = L.make_icon(alt, logo_dir())
            finally:
                try:
                    alt.unlink()
                except Exception:
                    pass
    except ValueError as e:
        return f"Sir, {e}"
    except Exception as e:
        return f"Sir, I couldn't turn that picture into a logo: {e}"
    _cfg_save({"file": ico.stem, "source": src.name})
    _cleanup(keep=ico.stem)
    win = getattr(player, "_win", None)
    if win is not None:
        apply_in_app(win, (ico, png))
        _refresh_buttons(player)
    _sync_bg(player, announce=True)
    return (f"Done, {src.name} is my new logo: the window, the taskbar and my shortcuts are "
            "updating now. If a pinned taskbar icon still shows the old one, it changes after "
            "the next sign-in.")


def reset_logo(player) -> str:
    had = current_logo() is not None
    _cfg_save({"file": "", "source": ""})
    win = getattr(player, "_win", None)
    if win is not None:
        apply_in_app(win, None)
        _refresh_buttons(player)
    if had or sys.platform == "win32":
        threading.Thread(target=lambda: (sync_now(player, announce=had), _cleanup(keep=None)),
                         daemon=True, name="custom-logo-reset").start()
    return "Back to my normal Habibi logo." if had else "I'm already using my normal logo."


def pick_and_set(player) -> str:
    """File picker (Qt thread)."""
    from PyQt6.QtCore import QStandardPaths
    from PyQt6.QtWidgets import QFileDialog
    win = getattr(player, "_win", None)
    start = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
    pats = " ".join(f"*{e}" for e in L.IMAGE_TYPES + (".svg",))
    path, _ = QFileDialog.getOpenFileName(win, "Choose Habibi's new logo", start,
                                          f"Pictures ({pats});;All files (*)")
    if not path:
        return "No picture chosen, the logo stays as it is."
    return set_logo(player, path)


def status() -> str:
    logo = current_logo()
    if not logo:
        return "I'm using my normal Habibi logo."
    src = _cfg().get("source") or logo[0].name
    return f"My logo is your picture {src}."


# ══════════════════════════════════════════════════════════════════════════════
#  Plugin hooks
# ══════════════════════════════════════════════════════════════════════════════

def setup_ui(player) -> None:
    """Runs once on the Qt thread: put the saved logo on, add the ⚙ buttons,
    and keep the shortcuts in line (now and every 10 minutes)."""
    win = getattr(player, "_win", None)
    if win is None:
        return
    logo = current_logo()
    if logo:
        apply_in_app(win, logo)
    _add_buttons(player)
    _sync_bg(player)
    from PyQt6.QtCore import QTimer
    t = getattr(win, "_custom_logo_timer", None)
    if t is None:
        t = QTimer(win)
        t.setInterval(RESYNC_MS)
        win._custom_logo_timer = t
    else:
        try:
            t.timeout.disconnect()
        except Exception:
            pass
    t.timeout.connect(lambda: _sync_bg(player))
    t.start()


def _on_ui(player, fn):
    if threading.current_thread() is threading.main_thread() or player is None \
            or not hasattr(player, "run_in_ui"):
        return fn()
    return player.run_in_ui(fn, timeout=600)


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    path = str(p.get("path") or "").strip()
    try:
        if action in ("reset", "default", "normal", "remove", "off"):
            return _on_ui(player, lambda: reset_logo(player))
        if action == "status":
            return status()
        if action == "pick" or (action in ("", "set", "change") and not path):
            if not path and action != "pick":
                dropped = getattr(player, "current_file", None) if player else None
                if dropped and Path(dropped).suffix.lower() in L.IMAGE_TYPES + (".svg",):
                    path = dropped
            if not path:
                if player is None:
                    return "Sir, tell me which picture to use."
                return _on_ui(player, lambda: pick_and_set(player))
        return _on_ui(player, lambda: set_logo(player, path))
    except Exception as e:
        return f"Sir, changing the logo failed: {e}"
