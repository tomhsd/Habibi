"""
Plugin discovery, validation, collision detection, and dispatch.

Discovery runs once (HabibiLive.__init__ calls discover_plugins()); the resulting
PluginRegistry is cached for the process lifetime. Enable/disable state is re-read
from config on every call to get_tool_declarations() / run() / list_for_ui(), so
toggling a plugin does not require restarting the app or re-importing anything.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from memory.config_manager import get_plugin_enabled, get_plugin_config

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}


@dataclass
class PluginRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    run: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    error: str = ""
    settings: Optional[dict] = None   # optional PLUGIN_SETTINGS schema (config fields)
    ui_setup: Optional[Callable] = None   # optional setup_ui(player): runs once on the Qt thread
    # optional voice takeover (e.g. offline mode): see PluginRegistry.voice_takeover
    voice_wants: Optional[Callable] = None     # voice_takeover(host, failures) -> bool
    voice_session: Optional[Callable] = None   # async voice_session(host): runs until it hands back
    sleep_listener: Optional[Callable] = None  # sleep_listener(host) -> feed(frame) | None (wake words)
    mtime: float = 0.0


class PluginRegistry:
    def __init__(self, plugins: dict[str, PluginRecord], logger: Callable[[str], None]):
        self._plugins = plugins          # name -> PluginRecord, VALID entries only
        self._all_records: list[PluginRecord] = []   # valid + invalid, for UI listing
        self._logger = logger
        self._dir: Optional[Path] = None
        self._core: set[str] = set()
        self._ui_done: set[str] = set()   # plugins whose setup_ui already ran


    # -- live management: no restart needed -------------------------------
    def reload(self, initial: bool = False) -> dict:
        """Re-scan the plugins folder. New or edited files are imported, files
        that vanished are dropped, untouched plugins keep running as they are.
        Returns {"added": [...], "removed": [...], "changed": [...]}."""
        if self._dir is None:
            return {"added": [], "removed": [], "changed": []}
        self._dir.mkdir(parents=True, exist_ok=True)
        old = {r.file: r for r in self._all_records}
        files = [p for p in sorted(self._dir.glob("*.py"), key=lambda p: p.name)
                 if not p.name.startswith("_")]
        valid: dict[str, PluginRecord] = {}
        records: list[PluginRecord] = []
        added, changed = [], []
        for path in files:
            prev = old.get(path.name)
            try:
                mt = path.stat().st_mtime
            except OSError:
                continue
            if prev is not None and prev.mtime == mt and (not prev.valid or prev.name not in valid):
                rec = prev
            else:
                rec = _load_file(path, self._core, valid)
                if prev is None:
                    added.append(rec.name)
                else:
                    changed.append(rec.name)
                if not initial:
                    self._logger(f"Plugin {'loaded' if rec.valid else 'rejected'}: "
                                 f"{path.name}" + ("" if rec.valid else f" — {rec.error}"))
            records.append(rec)
            if rec.valid:
                valid[rec.name] = rec
                if initial:
                    self._logger(f"Plugin loaded: {rec.name} ({path.name})")
            elif initial:
                self._logger(f"Plugin rejected: {path.name} — {rec.error}")
        gone = [r.name for f, r in old.items()
                if f not in {p.name for p in files} and r.valid]
        for f in old:
            if f not in {p.name for p in files}:
                sys.modules.pop(f"plugins.{Path(f).stem}", None)
        self._plugins.clear()
        self._plugins.update(valid)
        self._all_records = records
        if initial:
            self._logger(f"Plugin discovery complete: {len(valid)} active, "
                         f"{len(records) - len(valid)} rejected, {len(records)} total.")
        return {"added": added, "removed": gone, "changed": changed}

    def install(self, src_path: str) -> dict:
        """Copy a .py file into the plugins folder and load it now. If the file
        turns out not to be a valid plugin the folder is put back as it was."""
        import shutil
        src = Path(src_path)
        if self._dir is None or not src.is_file() or src.suffix.lower() != ".py":
            return {"ok": False, "message": "Pick a .py plugin file."}
        if src.name.startswith("_"):
            return {"ok": False, "message": "File names starting with '_' are ignored by the loader."}
        dest = self._dir / src.name
        backup = None
        if dest.exists():
            backup = self._dir / "_removed" / f"{dest.stem}.{int(time.time())}.bak"
            backup.parent.mkdir(exist_ok=True)
            shutil.move(str(dest), str(backup))
        shutil.copyfile(str(src), str(dest))
        self.reload()
        rec = next((r for r in self._all_records if r.file == dest.name), None)
        if rec is None or not rec.valid:
            err = rec.error if rec else "unknown error"
            dest.unlink(missing_ok=True)
            if backup is not None:
                shutil.move(str(backup), str(dest))
            self.reload()
            return {"ok": False, "message": f"Not installed — {err}"}
        return {"ok": True, "name": rec.name,
                "message": f"Installed '{rec.name}'" + (" (replaced the old version)." if backup else ".")}

    def remove(self, name: str) -> dict:
        """Take a plugin out of service now. The file moves to plugins/_removed/
        so it can be put back by hand."""
        import shutil
        rec = next((r for r in self._all_records if r.file == name or r.name == name), None)
        if rec is None or self._dir is None:
            return {"ok": False, "message": f"No plugin called '{name}'."}
        src = self._dir / rec.file
        if src.exists():
            dst = self._dir / "_removed" / f"{src.stem}.{int(time.time())}.py"
            dst.parent.mkdir(exist_ok=True)
            shutil.move(str(src), str(dst))
        self.reload()
        return {"ok": True, "name": name,
                "message": f"Removed '{name}'. Its file is in plugins/_removed/."}

    # -- called by main.py at LiveConnectConfig build time --
    def get_tool_declarations(self) -> list[dict]:
        decls = []
        for name, rec in self._plugins.items():
            if get_plugin_enabled(name):
                decls.append({
                    "name": rec.name,
                    "description": rec.description,
                    "parameters": rec.parameters,
                })
        return decls

    def has(self, name: str) -> bool:
        return name in self._plugins

    # -- called by main.py from _execute_tool's else branch --
    def run(self, name: str, parameters: dict, player=None, session_memory=None) -> str:
        rec = self._plugins.get(name)
        if rec is None or not rec.valid:
            return f"Plugin '{name}' is not available."
        if not get_plugin_enabled(name):
            return f"The '{name}' plugin is currently disabled."
        # Core safety supervises every plugin: emergency stop, blocked apps/folders
        # (PermissionError) and the activity log — plugins can't switch these off.
        from core import safety
        if safety.stopped():
            return "🛑 Emergency stop is active — wait a few seconds, then ask again."
        try:
            result = _call_run(rec.run, parameters, player, session_memory) or "Done."
        except safety.Stopped:
            result = "🛑 Stopped by the emergency stop (Ctrl+Alt+Q)."
        except PermissionError as e:
            result = f"Not allowed: {e}"
        except Exception as e:
            self._logger(f"Plugin '{name}' crashed during run(): {e}")
            traceback.print_exc()
            result = f"Sir, the '{name}' plugin failed: {e}"
        try:
            # A plugin holding private data (money, health…) sets PRIVATE_LOG = True:
            # the activity log then records only which action ran, never the values.
            if getattr(rec.run, "__globals__", {}).get("PRIVATE_LOG") is True:
                safety.log_activity(name, {"action": str((parameters or {}).get("action", ""))[:40]},
                                    "(private: details not logged)")
            else:
                safety.log_activity(name, parameters, str(result))
        except Exception:
            pass
        return result

    # -- called by main.py once the window exists --
    def run_ui_setup(self, player) -> None:
        """Call each enabled plugin's optional setup_ui(player) on the Qt thread,
        so a plugin can add buttons or views to the interface. Never raises."""
        if player is None:
            return
        for name, rec in self._plugins.items():
            if rec.ui_setup is None or name in self._ui_done or not get_plugin_enabled(name):
                continue
            self._ui_done.add(name)
            try:
                runner = getattr(player, "run_in_ui", None)
                if runner is not None:
                    runner(lambda f=rec.ui_setup: f(player))
                else:
                    rec.ui_setup(player)
            except Exception as e:
                self._logger(f"Plugin '{name}' setup_ui failed: {e}")
                traceback.print_exc()

    # -- called by main.py once: plugins that listen while Habibi sleeps --
    def sleep_listeners(self, host) -> list[Callable]:
        """For enabled plugins that define sleep_listener(host): call it and collect
        the feed(int16_frame) callables it returns (None = not available). The core
        hands every mic frame heard while asleep to each feed; a plugin wakes Habibi
        with host.wake(reason). Used for extra wake words. Never raises."""
        out: list[Callable] = []
        for name, rec in self._plugins.items():
            if rec.sleep_listener is None or not get_plugin_enabled(name):
                continue
            try:
                feed = rec.sleep_listener(host)
                if callable(feed):
                    out.append(feed)
            except Exception as e:
                self._logger(f"Plugin '{name}' sleep_listener failed: {e}")
                traceback.print_exc()
        return out

    # -- called by main.py before each Gemini connect: optional voice takeover --
    def voice_takeover(self, host, failures: int = 0) -> Optional[PluginRecord]:
        """Ask enabled plugins that declare BOTH voice_takeover(host, failures) and
        async voice_session(host) whether they want to run the voice loop instead
        of Gemini Live (offline mode is the first user). `failures` is how many
        Gemini connects in a row have failed with a network error. Returns the
        first plugin that says yes, else None. Runs off the event loop (main.py
        calls it through asyncio.to_thread), so a plugin may do a quick network
        check here. Never raises."""
        for name, rec in self._plugins.items():
            if rec.voice_wants is None or rec.voice_session is None:
                continue
            if not get_plugin_enabled(name):
                continue
            try:
                if rec.voice_wants(host, failures):
                    return rec
            except Exception as e:
                self._logger(f"Plugin '{name}' voice_takeover failed: {e}")
                traceback.print_exc()
        return None

    # -- called by ui.py's settings tab to render per-plugin config forms --
    def settings_schemas(self) -> list[dict]:
        """One entry per settings SECTION, for enabled plugins that declare a
        PLUGIN_SETTINGS schema. Sections are deduped by namespace so a suite of
        plugins sharing one namespace (e.g. the printer trio) shows a single
        form. Current stored values are merged in so the UI can pre-fill fields.
        """
        seen: set[str] = set()
        out: list[dict] = []
        for name, rec in self._plugins.items():
            if not rec.settings or not get_plugin_enabled(name):
                continue
            ns = rec.settings.get("namespace") or rec.name
            if ns in seen:
                continue
            seen.add(ns)
            out.append({
                "plugin":    rec.name,
                "namespace": ns,
                "title":     rec.settings.get("title") or rec.name,
                "fields":    rec.settings.get("fields", []),
                "values":    get_plugin_config(ns),
                "action":    rec.settings.get("action"),   # optional test/connect button
            })
        return out

    # -- called by ui.py's Plugin Manager overlay --
    def list_for_ui(self) -> list[dict]:
        out = []
        for rec in self._all_records:
            out.append({
                "name": rec.name,
                "description": rec.description,
                "file": rec.file,
                "valid": rec.valid,
                "error": rec.error,
                "enabled": get_plugin_enabled(rec.name) if rec.valid else False,
            })
        return out


def _call_run(run_fn, parameters, player, session_memory):
    """Invoke run() passing only the kwargs it actually declares (or all of them
    if it has **kwargs), so a minimal `def run(parameters):` plugin still works."""
    sig = inspect.signature(run_fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    if has_var_kw or "player" in sig.parameters:
        kwargs["player"] = player
    if has_var_kw or "session_memory" in sig.parameters:
        kwargs["session_memory"] = session_memory
    return run_fn(parameters, **kwargs)


def _validate(module, filename: str) -> PluginRecord:
    """Returns a PluginRecord; .valid=False + .error set on any problem. Never raises."""
    plugin_meta = getattr(module, "PLUGIN", None)
    if not isinstance(plugin_meta, dict):
        return PluginRecord(name=Path(filename).stem, file=filename,
                             error="Missing PLUGIN dict constant.")

    name = plugin_meta.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return PluginRecord(name=str(name or Path(filename).stem), file=filename,
                             error="PLUGIN['name'] missing or not a valid identifier "
                                   "(letters/digits/underscore, must start with letter/underscore).")

    description = plugin_meta.get("description")
    if not isinstance(description, str) or not description.strip():
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['description'] missing or empty.")

    parameters = plugin_meta.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['parameters'] must be a dict with \"type\": \"OBJECT\".")

    run_fn = getattr(module, "run", None)
    if not callable(run_fn):
        return PluginRecord(name=name, file=filename,
                             error="Missing callable run(parameters, ...) function.")

    # Optional, self-describing settings schema (rendered by the settings UI).
    # A malformed schema is ignored, never fatal — the plugin still loads.
    settings = getattr(module, "PLUGIN_SETTINGS", None)
    if not (isinstance(settings, dict) and isinstance(settings.get("fields"), list)):
        settings = None

    ui_setup = getattr(module, "setup_ui", None)
    if not callable(ui_setup):
        ui_setup = None

    sleep_listener = getattr(module, "sleep_listener", None)
    if not callable(sleep_listener):
        sleep_listener = None

    # Optional voice takeover: both halves are needed, and the session must be async.
    voice_wants = getattr(module, "voice_takeover", None)
    voice_session = getattr(module, "voice_session", None)
    if not (callable(voice_wants) and inspect.iscoroutinefunction(voice_session)):
        voice_wants = voice_session = None

    return PluginRecord(name=name, description=description.strip(), parameters=parameters,
                         run=run_fn, file=filename, valid=True, error="", settings=settings,
                         ui_setup=ui_setup, voice_wants=voice_wants, voice_session=voice_session,
                         sleep_listener=sleep_listener)


def _load_file(path: Path, core_tool_names: set[str], valid: dict) -> PluginRecord:
    """Import one plugin file and validate it. Never raises."""
    try:
        module_name = f"plugins.{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError("could not build import spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise

        rec = _validate(module, path.name)

        if rec.valid and rec.name in core_tool_names:
            rec = PluginRecord(name=rec.name, file=path.name,
                                error=f"Name '{rec.name}' collides with a core tool — rejected.")
        elif rec.valid and rec.name in valid:
            other = valid[rec.name].file
            rec = PluginRecord(name=rec.name, file=path.name,
                                error=f"Name '{rec.name}' already used by plugin '{other}' — rejected.")
    except Exception as e:
        rec = PluginRecord(name=path.stem, file=path.name, error=f"Failed to load: {e}")
        traceback.print_exc()
    try:
        rec.mtime = path.stat().st_mtime
    except OSError:
        pass
    return rec


def discover_plugins(plugins_dir: Path, core_tool_names: set[str],
                      logger: Callable[[str], None] = print) -> PluginRegistry:
    """
    Scans plugins_dir for *.py files (skips files starting with '_', e.g. __init__.py,
    _template.py, and any shared-helper modules an author prefixes with '_').
    Import errors, validation errors, and name collisions are logged and the offending
    file is skipped — they NEVER raise out of this function and never abort the scan
    of remaining files.
    """
    plugins_dir.mkdir(parents=True, exist_ok=True)
    registry = PluginRegistry({}, logger)
    registry._dir = plugins_dir
    registry._core = set(core_tool_names)
    registry.reload(initial=True)
    return registry
