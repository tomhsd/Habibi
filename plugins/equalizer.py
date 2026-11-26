"""
plugins/equalizer.py — a full sound equalizer for Habibi's media player.

  • 10-band graphic EQ (31 Hz … 16 kHz, ±12 dB) with a live response curve
  • BASS (low shelf, up to +15 dB), TREBLE, PREAMP
  • BEAT: punch for kick drums (hits get stronger, the bass doesn't get boomy)
  • BALANCE (left/right), stereo WIDTH (mono … extra wide), LOUDNESS, LIMITER
  • 20 presets (Bass Boost, Deep Bass, Bass & Beat, Party, Hip-Hop, EDM, Rock,
    Pop, Bollywood, Vocal, Podcast, Movie, Late Night …) + your own presets
  • voice: "more bass", "beat 70", "rock preset", "60 hertz plus 4",
    "balance a bit left", "equalizer off", "save this as my car preset"

Where it works
  • files played by Habibi's player: Qt hands the decoded audio to the
    plugin (ui_media audio-processor hook, Qt 6.8+), it is processed with numpy
    and played through its own audio output.
  • YouTube in the player: a Web Audio graph with the same filters is injected
    into the YouTube player frame.
Not: sound from other apps or browsers (Spotify, Chrome…) — that would need a
system-wide audio driver such as Equalizer APO.

UI: a 🎚 EQ button on the MEDIA PLAYER card opens the panel inside the card.
Settings and your presets are saved in config/equalizer.json.
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path


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


D = _sibling("_eq_dsp")


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _store_path() -> Path:
    return _base_dir() / "config" / "equalizer.json"


# ══════════════════════════════════════════════════════════════════════════════
#  Controller: settings, presets, saving, and both sound paths
# ══════════════════════════════════════════════════════════════════════════════

class Equalizer:
    def __init__(self, engine=None, path: Path | None = None):
        self.engine = engine
        self.path = path or _store_path()
        self.custom: dict[str, dict] = {}
        self.settings = D.normalise(None)
        self._load()
        self.proc = D.Processor(self.settings)
        self.panel = None
        self.web_ack = None           # last answer from the YouTube player frame
        self._web_installed = False
        self._web_sent = None
        self._save_t = None
        self._web_t = None

    # ── storage ──────────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        self.settings = D.normalise(data.get("settings"))
        cp = data.get("custom_presets") or {}
        if isinstance(cp, dict):
            self.custom = {str(k)[:40]: D.normalise(v) for k, v in cp.items() if isinstance(v, dict)}

    def save_now(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"settings": self.settings, "custom_presets": self.custom},
                                      indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            print(f"[Equalizer] couldn't save settings: {e}")

    def _save_soon(self) -> None:
        if self._save_t is None:
            try:
                from PyQt6.QtCore import QCoreApplication, QTimer
                if QCoreApplication.instance() is None:
                    raise RuntimeError("no Qt app")
                self._save_t = QTimer()
                self._save_t.setSingleShot(True)
                self._save_t.setInterval(700)
                self._save_t.timeout.connect(self.save_now)
            except Exception:
                self.save_now()
                return
        self._save_t.start()

    # ── changes ──────────────────────────────────────────────────────────────
    def preset_names(self) -> list[str]:
        return list(D.PRESETS) + [n for n in self.custom if n not in D.PRESETS]

    def _commit(self, new: dict, mark_custom: bool = True) -> None:
        new = D.normalise(new)
        if mark_custom:
            ref = self._preset_dict(new["preset"])
            if ref is None or not self._matches(new, ref):
                new["preset"] = "Custom"
        self.settings = new
        self.proc.set(new)
        if self.engine is not None:
            try:
                self.engine.processor_changed()
            except Exception:
                pass
        self._push_web()
        self._save_soon()
        if self.panel is not None:
            self.panel.show_settings(self.settings, self.preset_names())

    def _preset_dict(self, name: str):
        if name in self.custom:
            return self.custom[name]
        return D.PRESETS.get(name)

    def _matches(self, s: dict, ref: dict) -> bool:
        want = D.apply_preset(s, ref, s["preset"])
        keys = ("bands", "bass", "treble", "beat", "width", "loudness")
        return all(want[k] == s[k] for k in keys)

    def update(self, changes: dict) -> None:
        s = dict(self.settings)
        s.update(changes)
        if any(k not in ("enabled", "limiter", "balance", "preamp") for k in changes):
            s["enabled"] = True if "enabled" not in changes else s["enabled"]
        self._commit(s)

    def preset(self, name: str) -> str | None:
        key = self.find_preset(name)
        if key is None:
            return None
        self._commit(D.apply_preset(self.settings, self._preset_dict(key), key), mark_custom=False)
        return key

    def find_preset(self, name: str) -> str | None:
        def k(x):
            return re.sub(r"[^a-z0-9]", "", x.lower().replace("&", "and"))
        q = k(name or "")
        if not q:
            return None
        names = self.preset_names()
        for n in names:
            if k(n) == q:
                return n
        aliases = {"normal": "Flat", "default": "Flat", "none": "Flat", "off": "Flat", "neutral": "Flat",
                   "bass": "Bass Boost", "bassboost": "Bass Boost", "morebass": "Bass Boost",
                   "subbass": "Deep Bass", "heavybass": "Deep Bass", "superbass": "Deep Bass",
                   "beat": "Bass & Beat", "beats": "Bass & Beat", "bassbeat": "Bass & Beat",
                   "club": "Party", "dj": "Party", "hiphop": "Hip-Hop", "rap": "Hip-Hop",
                   "dance": "EDM", "electronic": "EDM", "techno": "EDM", "house": "EDM",
                   "metal": "Rock", "bangla": "Bollywood", "bengali": "Bollywood", "hindi": "Bollywood",
                   "indian": "Bollywood", "voice": "Vocal", "vocals": "Vocal", "singer": "Vocal",
                   "speech": "Podcast", "talk": "Podcast", "audiobook": "Podcast", "news": "Podcast",
                   "treble": "Treble Boost", "bright": "Treble Boost", "cinema": "Movie", "film": "Movie",
                   "movies": "Movie", "headphone": "Headphones", "earphones": "Headphones",
                   "laptop": "Laptop Speakers", "smallspeakers": "Laptop Speakers", "speakers": "Laptop Speakers",
                   "night": "Late Night", "quiet": "Late Night", "soft": "Late Night"}
        if q in aliases:
            return aliases[q]
        for n in names:
            if q in k(n) or k(n) in q:
                return n
        for a, n in aliases.items():
            if a in q:
                return n
        return None

    def save_preset(self, name: str) -> str:
        name = name.strip()[:40] or "My preset"
        if name in D.PRESETS:
            name += " (mine)"
        keep = {k: self.settings[k] for k in ("bands", "bass", "treble", "beat", "width", "loudness",
                                              "preamp", "balance", "limiter")}
        self.custom[name] = keep
        s = dict(self.settings)
        s["preset"] = name
        self._commit(s, mark_custom=False)
        self.save_now()
        return name

    def delete_preset(self, name: str) -> str | None:
        key = self.find_preset(name)
        if key is None or key not in self.custom:
            return None
        del self.custom[key]
        if self.settings["preset"] == key:
            s = dict(self.settings); s["preset"] = "Custom"
            self._commit(s, mark_custom=False)
        self.save_now()
        return key

    def reset(self) -> None:
        s = D.normalise({"limiter": self.settings["limiter"]})
        self._commit(s, mark_custom=False)

    # ── YouTube (Web Audio in the player frame) ──────────────────────────────
    def _yt(self):
        src = getattr(self.engine, "src", None)
        if src is None or not hasattr(src, "profile") or not hasattr(src, "view"):
            return None
        return src

    def install_web(self) -> bool:
        """Put the Web Audio script into the YouTube profile (all frames). Must
        happen before the player page loads — it loads on the first YouTube play."""
        src = self._yt()
        if src is None or self._web_installed:
            return self._web_installed
        try:
            from PyQt6.QtWebEngineCore import QWebEngineScript
            sc = QWebEngineScript()
            sc.setName("habibi-equalizer")
            sc.setSourceCode(D.WEB_JS)
            sc.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
            sc.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
            sc.setRunsOnSubFrames(True)
            src.profile.scripts().insert(sc)
            self._web_installed = True
            self._web_late = bool(getattr(src, "_loaded", False))
        except Exception as e:
            print(f"[Equalizer] YouTube EQ unavailable: {e}")
        return self._web_installed

    def _push_web(self, force: bool = False) -> None:
        src = self._yt()
        if src is None or not self._web_installed:
            return
        if not (getattr(src, "_loaded", False) and getattr(src, "_page_ready", False)):
            return
        payload = D.web_forward_js(self.settings)
        if payload == self._web_sent and not force:
            return
        self._web_sent = payload
        try:
            src.view.page().runJavaScript(payload, self._got_ack)
        except Exception:
            pass

    def _got_ack(self, raw) -> None:
        try:
            a = json.loads(raw) if raw else None
        except Exception:
            a = None
        if a:
            self.web_ack = a

    def start_web_sync(self) -> None:
        """Re-send the settings every few seconds while YouTube plays (a new
        player frame or video element picks them up)."""
        from PyQt6.QtCore import QTimer
        self._web_t = QTimer()
        self._web_t.timeout.connect(self._web_tick)
        self._web_t.start(2500)

    def _web_tick(self) -> None:
        if self._yt() is None:
            return
        if not self._web_installed:
            self.install_web()
        if getattr(self.engine, "mode", "") == "external":
            self._push_web(force=True)

    # ── status ───────────────────────────────────────────────────────────────
    def where_text(self) -> str:
        e = self.engine
        parts = []
        if e is not None and hasattr(e, "processing_status"):
            st = e.processing_status()
            if st.startswith("unavailable"):
                parts.append("Files: not possible with this Qt (needs 6.8+)")
            elif st.startswith("failed"):
                parts.append("Files: " + st)
            elif st == "on":
                parts.append("Files: EQ on")
            else:
                parts.append("Files: ready" if D.wants_processing(self.settings) else "Files: flat (bypassed)")
        if self._yt() is not None:
            if not self._web_installed:
                parts.append("YouTube: not ready")
            elif getattr(self, "_web_late", False) and not self.web_ack:
                parts.append("YouTube: restart Habibi once")
            elif self.web_ack and self.web_ack.get("videos"):
                parts.append("YouTube: EQ on")
            else:
                parts.append("YouTube: ready")
        return " · ".join(parts)

    def status(self) -> str:
        return D.describe(self.settings) + " Works on: " + (self.where_text() or "—") + "."


_ctl: Equalizer | None = None
_btn = None


# ══════════════════════════════════════════════════════════════════════════════
#  UI
# ══════════════════════════════════════════════════════════════════════════════

def setup_ui(player) -> None:
    """Runs once on the Qt thread: EQ button + panel on the MEDIA PLAYER card,
    and the equalizer in the player's sound path."""
    global _ctl, _btn
    engine = getattr(player, "media_engine", None)
    card = getattr(player, "media_card", None)
    if engine is None or card is None or _ctl is not None:
        return
    if not hasattr(engine, "set_processor"):
        print("[Equalizer] this Habibi's media player has no audio-processor hook")
        return
    _ctl = Equalizer(engine)
    engine.set_processor(_ctl.proc)

    ui_mod = _sibling("_eq_ui")
    panel = ui_mod.EqPanel(_ctl)
    panel.setVisible(False)
    _ctl.panel = panel
    lay = card._body_lay
    i = lay.indexOf(card.listw)
    lay.insertWidget(i if i >= 0 else lay.count(), panel)
    _btn = card.add_source_button("🎚 EQ", "Equalizer: bands, bass, beat, treble, balance, presets",
                                  lambda: None)
    _btn.setCheckable(True)
    _btn.toggled.connect(panel.setVisible)
    panel.show_settings(_ctl.settings, _ctl.preset_names())

    from PyQt6.QtCore import QTimer
    QTimer.singleShot(0, _ctl.install_web)     # YouTube plugin attaches after this one
    _ctl.start_web_sync()


# ══════════════════════════════════════════════════════════════════════════════
#  Voice / AI
# ══════════════════════════════════════════════════════════════════════════════

def _band_index(v) -> int | None:
    """'60', '62 hz', '1k', '1 khz', '16000', 3 (= 3rd band) → band index."""
    if v is None or v == "":
        return None
    t = str(v).lower().replace(" ", "").replace("hertz", "").replace("hz", "")
    m = re.fullmatch(r"(?:band)?(\d+(?:\.\d+)?)(k|khz)?", t)
    if not m:
        return None
    x = float(m.group(1))
    if m.group(2):
        x *= 1000
    elif x <= 10 and x == int(x):
        i = int(x) - 1
        return i if 0 <= i < 10 else None
    if x <= 0:
        return None
    return min(range(10), key=lambda i: abs(math.log(D.FREQS[i] / x)))


def _num(p: dict, *keys, default=None):
    for k in keys:
        v = p.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            m = re.search(r"-?\d+(?:\.\d+)?", str(v))
            if m:
                return float(m.group(0))
    return default


def command(ctl: Equalizer, p: dict) -> str:
    """All voice/AI actions. Runs on the Qt thread."""
    action = str(p.get("action", "status")).lower().strip().replace(" ", "_")
    rel = bool(p.get("relative", False))
    s = ctl.settings

    def knob(key: str, default_step: float, unit: str) -> str:
        v = _num(p, "value", "level", "amount", "gain")
        if v is None:                                  # "more bass" with no number
            v, r = default_step, True
        else:
            r = rel
        new = D.clamp(key, (s[key] + v) if r else v)
        ctl.update({key: new})
        return f"{key.capitalize()} {new:+.0f}{unit}." if unit == " dB" else f"{key.capitalize()} {new:.0f}{unit}."

    if action in ("status", "show_settings", "get"):
        return ctl.status()
    if action in ("on", "enable"):
        ctl.update({"enabled": True}); return "Equalizer on. " + ctl.where_text()
    if action in ("off", "disable", "bypass"):
        ctl.update({"enabled": False}); return "Equalizer off: the sound is unprocessed now."
    if action == "toggle":
        on = p.get("on")
        on = (not s["enabled"]) if on is None else bool(on)
        ctl.update({"enabled": on}); return f"Equalizer {'on' if on else 'off'}."
    if action in ("presets", "list_presets"):
        mine = [n for n in ctl.custom]
        return ("Presets: " + ", ".join(D.PRESETS) + "." +
                (f" Your presets: {', '.join(mine)}." if mine else "") + f" Current: {s['preset']}.")
    if action == "preset":
        name = str(p.get("preset") or p.get("name") or "")
        key = ctl.preset(name)
        if key is None:
            return f"There's no preset called '{name}'. Presets: {', '.join(ctl.preset_names())}."
        return f"{key} preset on."
    if action in ("band", "set_band"):
        i = _band_index(p.get("band"))
        if i is None:
            return "Which band? Say a frequency like 60 Hz, 1k or 16k, or a band number 1 to 10."
        v = _num(p, "gain", "value", "level", "amount")
        if v is None:
            v, r = 3.0, True
        else:
            r = rel
        bands = list(s["bands"])
        bands[i] = D.clamp("band", (bands[i] + v) if r else v)
        ctl.update({"bands": bands})
        return f"{D.BAND_LABELS[i]} Hz band {bands[i]:+.0f} dB."
    if action in ("bands", "set_bands"):
        raw = p.get("bands") or p.get("value") or ""
        vals = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", str(raw))]
        if len(vals) != 10:
            return "Give all 10 band gains in dB, from 31 Hz to 16 kHz, e.g. '6,5,3,0,-1,0,1,2,3,3'."
        ctl.update({"bands": vals})
        return "Bands set: " + ", ".join(f"{l} {g:+.0f}" for l, g in zip(D.BAND_LABELS, ctl.settings["bands"])) + "."
    if action == "bass":
        return knob("bass", 3.0, " dB")
    if action == "treble":
        return knob("treble", 3.0, " dB")
    if action == "preamp":
        return knob("preamp", 2.0, " dB")
    if action in ("beat", "punch"):
        return knob("beat", 20.0, "%")
    if action == "width":
        return knob("width", 25.0, "%")
    if action == "balance":
        v = _num(p, "value", "level", "amount")
        if v is None:
            return "Say how much: balance -100 is fully left, 0 centre, 100 fully right."
        new = D.clamp("balance", (s["balance"] + v) if rel else v)
        ctl.update({"balance": new})
        return "Balance centre." if abs(new) < 0.5 else f"Balance {abs(new):.0f}% {'right' if new > 0 else 'left'}."
    if action in ("loudness", "limiter"):
        on = p.get("on")
        on = (not s[action]) if on is None else bool(on)
        ctl.update({action: on})
        return f"{action.capitalize()} {'on' if on else 'off'}."
    if action in ("reset", "flat"):
        ctl.reset(); return "Equalizer reset to flat."
    if action == "save_preset":
        name = str(p.get("name") or p.get("preset") or "").strip()
        if not name:
            return "What should I call the preset?"
        return f"Saved your settings as the preset '{ctl.save_preset(name)}'."
    if action == "delete_preset":
        name = str(p.get("name") or p.get("preset") or "")
        key = ctl.delete_preset(name)
        return f"Deleted your preset '{key}'." if key else f"You have no saved preset called '{name}'."
    if action in ("show", "open", "hide", "close"):
        if _btn is None:
            return "The equalizer panel is only in the Habibi window."
        _btn.setChecked(action in ("show", "open"))
        return "Equalizer panel " + ("open." if action in ("show", "open") else "closed.")
    return ("Unknown equalizer action. Use status, on, off, preset, presets, band, bands, bass, treble, "
            "beat, preamp, balance, width, loudness, limiter, reset, save_preset, delete_preset, show, hide.")


PLUGIN = {
    "name": "equalizer",
    "description": (
        "Sound EQUALIZER for Habibi's built-in media player (local music/videos and YouTube in the "
        "player). Use for anything about how the music SOUNDS: bass, beat/punch, treble, equalizer "
        "bands, presets, balance, stereo width, loudness. Actions: status; on/off; preset(preset: "
        "Flat, Bass Boost, Deep Bass, Bass & Beat, Party, Hip-Hop, EDM, Rock, Pop, Bollywood, Jazz, "
        "Classical, Acoustic, Vocal, Podcast, Treble Boost, Movie, Headphones, Laptop Speakers, Late "
        "Night or a saved one); presets (list); band(band = frequency like '60', '1k', '16k' or 1-10, "
        "gain dB -12..12); bands(bands = 10 comma-separated dB values 31Hz..16kHz); bass(value dB "
        "-12..15); treble(value dB -12..12); beat(value 0-100 = kick-drum punch); preamp(value dB); "
        "balance(value -100 left..100 right); width(value 0 mono..100 normal..200 wide); "
        "loudness(on); limiter(on); reset; save_preset(name); delete_preset(name); show/hide (panel). "
        "Set relative=true for 'more/less' ('more bass' = bass value 3 relative; 'less beat' = beat "
        "value -20 relative). Volume, play, pause etc. belong to media_player, not this tool."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "status | on | off | preset | presets | band | bands | bass | treble | beat | preamp | balance | width | loudness | limiter | reset | save_preset | delete_preset | show | hide"},
            "preset": {"type": "STRING", "description": "preset: preset name, e.g. 'bass boost', 'rock', 'party'"},
            "name": {"type": "STRING", "description": "save_preset/delete_preset: the user's preset name"},
            "band": {"type": "STRING", "description": "band: frequency ('31', '62', '125', '250', '500', '1k', '2k', '4k', '8k', '16k') or band number 1-10"},
            "gain": {"type": "NUMBER", "description": "band: gain in dB (-12..12), or the change if relative"},
            "bands": {"type": "STRING", "description": "bands: 10 comma-separated dB gains from 31 Hz to 16 kHz"},
            "value": {"type": "NUMBER", "description": "bass/treble/preamp (dB), beat (0-100), balance (-100..100), width (0-200); or the change if relative"},
            "relative": {"type": "BOOLEAN", "description": "change by value/gain instead of setting it"},
            "on": {"type": "BOOLEAN", "description": "loudness/limiter: on or off"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Plugin entry point (switch this tool off in ⚙ PLUGINS)."""
    p = parameters or {}
    if _ctl is None or player is None:
        return "The equalizer works in the Habibi window's media player, which isn't open."
    try:
        runner = getattr(player, "run_in_ui", None)
        r = runner(lambda: command(_ctl, p)) if runner else command(_ctl, p)
    except Exception as e:
        return f"Sir, the equalizer failed: {e}"
    try:
        player.write_log(f"HABIBI: {r}")
    except Exception:
        pass
    return r
