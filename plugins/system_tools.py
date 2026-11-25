"""
plugins/system_tools.py — deeper Windows control: running programs, per-app
volume, startup apps, cleaning temporary files, power plans, Bluetooth,
battery, disks and network.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from core import automation as A
from core import safety

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_DISABLED_KEY = r"Software\Habibi\DisabledStartup"


def _ps(script: str, timeout: int = 30) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=timeout)
    return (r.stdout or r.stderr or "").strip()


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ── processes ────────────────────────────────────────────────────────────────

def _processes(sort: str, n: int) -> str:
    import psutil
    procs = list(psutil.process_iter(["pid", "name", "memory_info"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    time.sleep(0.6)
    rows = []
    cores = psutil.cpu_count() or 1
    for p in procs:
        try:
            rows.append((p.info["name"], p.pid, p.cpu_percent(None) / cores, p.info["memory_info"].rss))
        except Exception:
            pass
    rows.sort(key=lambda r: r[3] if sort == "memory" else r[2], reverse=True)
    vm = psutil.virtual_memory()
    head = f"CPU {psutil.cpu_percent(None):.0f}% · RAM {vm.percent:.0f}% used ({_fmt_bytes(vm.used)} of {_fmt_bytes(vm.total)})\n"
    return head + "\n".join(f"• {name} (pid {pid}) — CPU {cpu:.0f}%, RAM {_fmt_bytes(mem)}"
                            for name, pid, cpu, mem in rows[:n])


def _kill(target: str) -> str:
    import psutil
    if not target:
        return "Which program? (name or pid)"
    victims = []
    for p in psutil.process_iter(["pid", "name"]):
        if (target.isdigit() and p.pid == int(target)) or p.info["name"].lower() in (target.lower(), target.lower() + ".exe"):
            victims.append(p)
    if not victims:
        return f"No running program called '{target}'."
    protected = {"explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe", "services.exe",
                 "svchost.exe", "dwm.exe", "system", "smss.exe", "wininit.exe"}
    if any(v.info["name"].lower() in protected for v in victims):
        return f"'{target}' is part of Windows — I won't close it."
    names = ", ".join(sorted({v.info['name'] for v in victims}))

    def _do() -> str:
        for v in victims:
            try:
                v.terminate()
            except Exception:
                pass
        gone, alive = psutil.wait_procs(victims, timeout=3)
        for v in alive:
            try:
                v.kill()
            except Exception:
                pass
        return f"Closed {names} ({len(victims)} process{'es' if len(victims) > 1 else ''})."
    return safety.guarded("destructive", f"Force-close {names}", "Unsaved work in it will be lost", _do)


# ── per-app volume ───────────────────────────────────────────────────────────

def _app_volume(app: str, level, mute) -> str:
    from pycaw.pycaw import AudioUtilities
    sessions = [s for s in AudioUtilities.GetAllSessions() if s.Process]
    if not app:
        return "Apps playing sound: " + ", ".join(sorted({s.Process.name() for s in sessions})) if sessions else "No app is using sound right now."
    hits = [s for s in sessions if app.lower() in s.Process.name().lower()]
    if not hits:
        return f"'{app}' isn't playing sound right now."
    for s in hits:
        vol = s.SimpleAudioVolume
        if mute is not None:
            vol.SetMute(1 if mute else 0, None)
        if level is not None:
            vol.SetMasterVolume(max(0.0, min(1.0, int(level) / 100)), None)
    v = hits[0].SimpleAudioVolume
    return f"{hits[0].Process.name()}: volume {round(v.GetMasterVolume() * 100)}%" + (" (muted)" if v.GetMute() else "")


# ── startup apps ─────────────────────────────────────────────────────────────

def _startup_list() -> str:
    import winreg
    rows = []
    for hive, label in ((winreg.HKEY_CURRENT_USER, "you"), (winreg.HKEY_LOCAL_MACHINE, "all users")):
        try:
            with winreg.OpenKey(hive, _RUN_KEY) as k:
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(k, i)
                        rows.append(f"• {name} ({label})")
                        i += 1
                    except OSError:
                        break
        except OSError:
            pass
    folder = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup"
    for f in folder.glob("*") if folder.exists() else []:
        rows.append(f"• {f.stem} (startup folder)")
    return "Programs that start with Windows:\n" + "\n".join(rows) if rows else "No startup programs found."


def _startup_disable(name: str, enable: bool) -> str:
    import winreg
    src, dst = (_DISABLED_KEY, _RUN_KEY) if enable else (_RUN_KEY, _DISABLED_KEY)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, src, 0, winreg.KEY_ALL_ACCESS) as k:
            i, found = 0, None
            while True:
                try:
                    n, v, t = winreg.EnumValue(k, i)
                    if name.lower() in n.lower():
                        found = (n, v, t)
                        break
                    i += 1
                except OSError:
                    break
    except OSError:
        found = None
    if not found:
        return (f"'{name}' isn't in your own startup list" + ("" if enable else
                " (programs started for all users need administrator rights — use Task Manager → Startup apps).") )
    n, v, t = found

    def _do() -> str:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, dst) as k2:
            winreg.SetValueEx(k2, n, 0, t, v)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, src, 0, winreg.KEY_ALL_ACCESS) as k:
            winreg.DeleteValue(k, n)
        return f"'{n}' will {'now' if enable else 'no longer'} start with Windows" + ("" if enable else " (say 'enable it' to undo).")
    return safety.guarded("change", f"{'Enable' if enable else 'Disable'} startup: {n}", "", _do)


# ── temp clean ───────────────────────────────────────────────────────────────

def _temp_files():
    roots = {Path(tempfile.gettempdir())}
    win_tmp = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp"
    if os.access(win_tmp, os.W_OK):
        roots.add(win_tmp)
    cutoff = time.time() - 86400
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                p = Path(dirpath) / f
                try:
                    st = p.stat()
                    if st.st_mtime < cutoff:
                        yield p, st.st_size
                except OSError:
                    pass


def _clean_temp(do_it: bool) -> str:
    files = list(_temp_files())
    total = sum(s for _, s in files)
    if not do_it:
        return f"Temporary files older than a day: {len(files)} files, {_fmt_bytes(total)}. Say 'clean them' to delete."

    def _do() -> str:
        freed, n = 0, 0
        for p, s in files:
            safety.check_stop()
            try:
                p.unlink()
                freed += s
                n += 1
            except OSError:
                pass
        return f"Deleted {n} temporary files, freed {_fmt_bytes(freed)} (files in use were skipped)."
    return safety.guarded("destructive", "Delete temporary files", f"{len(files)} files, {_fmt_bytes(total)}", _do)


# ── power, bluetooth, battery, disks, network ────────────────────────────────

def _power(plan: str) -> str:
    out = subprocess.run(["powercfg", "/list"], capture_output=True, text=True).stdout
    plans = []
    for line in out.splitlines():
        if "GUID:" in line:
            guid = line.split("GUID:")[1].split()[0]
            name = line[line.find("(") + 1: line.find(")")]
            plans.append((guid, name, "*" in line))
    if not plan:
        return "Power plans: " + ", ".join(f"{n}{' (active)' if a else ''}" for _, n, a in plans)
    for guid, name, _ in plans:
        if plan.lower() in name.lower():
            subprocess.run(["powercfg", "/setactive", guid], capture_output=True)
            return f"Power plan set to {name}."
    return f"No power plan called '{plan}'."


_BT = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
Function Await($t, $type) { $n = $asTask.MakeGenericMethod($type).Invoke($null, @($t)); $n.Wait(-1) | Out-Null; $n.Result }
[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Radios.RadioAccessStatus,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$bt = $radios | ? { $_.Kind -eq 'Bluetooth' }
if (-not $bt) { 'NO_BLUETOOTH'; exit }
[Windows.Devices.Radios.RadioState,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
if ('STATE' -ne 'Status') { Await ($bt.SetStateAsync('STATE')) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null }
$bt.State
"""


def _bluetooth(state: str) -> str:
    st = {"on": "On", "off": "Off"}.get(state.lower(), "Status")
    out = _ps(_BT.replace("STATE", st))
    if "NO_BLUETOOTH" in out:
        return "This PC has no Bluetooth."
    return f"Bluetooth is {out.splitlines()[-1].strip().lower() if out else 'unknown'}."


def _battery() -> str:
    import psutil
    b = psutil.sensors_battery()
    if b is None:
        return "This PC has no battery (desktop)."
    left = "" if b.power_plugged or b.secsleft < 0 else f", about {b.secsleft // 3600}h {b.secsleft % 3600 // 60}m left"
    return f"Battery {b.percent:.0f}%{' — charging' if b.power_plugged else ''}{left}."


def _disks() -> str:
    import psutil
    rows = []
    for part in psutil.disk_partitions():
        try:
            u = shutil.disk_usage(part.mountpoint)
            rows.append(f"• {part.device} — {_fmt_bytes(u.free)} free of {_fmt_bytes(u.total)}")
        except Exception:
            pass
    return "Drives:\n" + "\n".join(rows)


def _network() -> str:
    import socket
    out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True).stdout
    ssid = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.strip().startswith("SSID")), "")
    signal = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.strip().startswith("Signal")), "")
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "?"
    return f"Wi-Fi: {ssid or 'not connected'}" + (f" ({signal})" if signal else "") + f" · IP {ip}"


def system_tools(parameters: dict, player=None) -> str:
    err = A.windows_only()
    if err:
        return err
    a = str(parameters.get("action", ""))
    target = str(parameters.get("target", "") or "")
    try:
        if a == "processes":
            return _processes(str(parameters.get("sort", "cpu")), int(parameters.get("count", 8) or 8))
        if a == "kill":
            return _kill(target)
        if a == "app_volume":
            lv = parameters.get("level")
            mute = parameters.get("mute")
            return _app_volume(target, lv, mute)
        if a == "startup_list":
            return _startup_list()
        if a in ("startup_disable", "startup_enable"):
            return _startup_disable(target, a == "startup_enable")
        if a == "clean_temp":
            return _clean_temp(bool(parameters.get("confirm_delete", False)))
        if a == "power_plan":
            return _power(target)
        if a == "bluetooth":
            return _bluetooth(target or "status")
        if a == "battery":
            return _battery()
        if a == "disks":
            return _disks()
        if a == "network":
            return _network()
        if a == "night_light":
            os.startfile("ms-settings:nightlight")
            return "Opened the Night light settings."
        return ("Actions: processes(sort cpu|memory, count), kill(target), app_volume(target, level, mute), "
                "startup_list, startup_disable(target), startup_enable(target), clean_temp(confirm_delete), "
                "power_plan(target), bluetooth(target on|off|status), battery, disks, network, night_light.")
    except ImportError as e:
        return f"Missing component: {e.name}"


PLUGIN = {
    "name": "system_tools",
    "description": (
        "Deeper Windows control. processes — what uses the most CPU/RAM (sort cpu|memory); "
        "kill(target) — force-close a program (asks first); app_volume(target=app name, level "
        "0-100, mute) — volume of ONE app, no target = list apps playing sound; startup_list, "
        "startup_disable(target), startup_enable(target); clean_temp — size of old temporary "
        "files, clean_temp(confirm_delete=true) deletes them (asks first); power_plan(target, "
        "blank = list); bluetooth(target on/off/status); battery; disks; network; night_light."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "processes | kill | app_volume | startup_list | startup_disable | startup_enable | clean_temp | power_plan | bluetooth | battery | disks | network | night_light"},
            "target": {"type": "STRING", "description": "Program / app / startup item / power plan name, or on/off for bluetooth"},
            "sort": {"type": "STRING", "description": "processes: cpu or memory"},
            "count": {"type": "INTEGER", "description": "processes: how many"},
            "level": {"type": "INTEGER", "description": "app_volume 0-100"},
            "mute": {"type": "BOOLEAN", "description": "app_volume: mute true/false"},
            "confirm_delete": {"type": "BOOLEAN", "description": "clean_temp: actually delete"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Plugin entry point (switch this tool off in ⚙ PLUGINS)."""
    return system_tools(parameters or {}, player=player)
