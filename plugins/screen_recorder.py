"""
plugins/screen_recorder.py — record the screen to an MP4 video.

"Start recording the screen" / "stop recording". Videos go to
Videos\\Habibi Recordings. Uses the ffmpeg that ships with Habibi
(imageio-ffmpeg). Ctrl+Alt+Q stops a recording too.
"""
from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from core import automation as A
from core import safety

_proc: subprocess.Popen | None = None
_out: Path | None = None
_started = 0.0
_timer: threading.Timer | None = None


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _stop() -> str:
    global _proc, _timer
    if _proc is None:
        return "No screen recording is running."
    p, out, secs = _proc, _out, time.time() - _started
    _proc = None
    if _timer:
        _timer.cancel()
        _timer = None
    try:
        p.stdin.write(b"q")
        p.stdin.flush()
        p.wait(timeout=15)
    except Exception:
        p.kill()
    if out and out.exists() and out.stat().st_size > 1000:
        return f"Recording saved ({secs:.0f} s): {out}"
    return "The recording failed — no video was saved."


safety.on_stop(lambda: _proc is not None and _stop())


def screen_recorder(parameters: dict, player=None) -> str:
    global _proc, _out, _started, _timer
    err = A.windows_only()
    if err:
        return err
    action = str(parameters.get("action", "status"))
    if action == "start":
        if _proc is not None:
            return f"Already recording ({time.time() - _started:.0f} s so far)."
        folder = Path.home() / "Videos" / "Habibi Recordings"
        folder.mkdir(parents=True, exist_ok=True)
        _out = folder / f"Screen {datetime.now():%Y-%m-%d %H-%M-%S}.mp4"
        fps = str(max(5, min(30, int(parameters.get("fps", 15) or 15))))
        try:
            exe = _ffmpeg()
        except Exception:
            return "Screen recording needs imageio-ffmpeg (pip install imageio-ffmpeg)."
        _proc = subprocess.Popen(
            [exe, "-y", "-loglevel", "error", "-f", "gdigrab", "-framerate", fps, "-draw_mouse", "1",
             "-i", "desktop", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", str(_out)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _started = time.time()
        limit = int(parameters.get("max_minutes", 30) or 30)
        _timer = threading.Timer(limit * 60, _stop)
        _timer.daemon = True
        _timer.start()
        time.sleep(0.8)
        if _proc.poll() is not None:
            _proc = None
            return "The screen recorder couldn't start."
        blk = safety.load().get("blocked_apps") or []
        note = " Remember: anything on screen is recorded" + (", including blocked apps." if blk else ".")
        return f"Recording the screen (stops by itself after {limit} min).{note}"
    if action == "stop":
        return _stop()
    if _proc is not None:
        return f"Recording — {time.time() - _started:.0f} s so far."
    return "Not recording."


PLUGIN = {
    "name": "screen_recorder",
    "description": (
        "Record the whole screen to an MP4 video (saved in Videos\\Habibi Recordings). "
        "start(fps, max_minutes), stop, status."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start | stop | status"},
            "fps": {"type": "INTEGER", "description": "Frames per second (5-30, default 15)"},
            "max_minutes": {"type": "INTEGER", "description": "Stop automatically after this (default 30)"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Plugin entry point (switch this tool off in ⚙ PLUGINS)."""
    return screen_recorder(parameters or {}, player=player)
