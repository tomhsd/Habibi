"""
plugins/media_player.py — play music and videos from the PC in Habibi's own
player (left sidebar), without a browser.

"Play my music", "play the Arijit songs in Downloads", "play D:\\Movies\\x.mkv",
"pause", "next", "volume 40", "skip 30 seconds", "shuffle on", "what's playing?"
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from core import safety

_SKIP_DIRS = {"appdata", "node_modules", "$recycle.bin", "windows", "program files",
              "program files (x86)", "programdata", ".git", "__pycache__", "site-packages"}


def _media_ext():
    from ui_media import MEDIA_EXT
    return MEDIA_EXT


def _roots() -> list[Path]:
    h = Path.home()
    cands = [h / "Music", h / "Videos", h / "Downloads", h / "Desktop", h / "Documents",
             h / "OneDrive" / "Music", h / "OneDrive" / "Videos", h / "OneDrive" / "Desktop"]
    if os.name == "nt":                        # other drives' top folders (D:\Music, E:\Movies…)
        import string
        for d in string.ascii_uppercase[3:]:
            p = Path(f"{d}:\\")
            if p.exists():
                cands.append(p)
    seen, out = set(), []
    for c in cands:
        try:
            r = c.resolve()
        except Exception:
            continue
        if c.exists() and r not in seen:
            seen.add(r)
            out.append(c)
    return out


def _words(q: str) -> list[str]:
    return [w for w in re.split(r"[\s_\-.,]+", q.lower()) if w]


def search(query: str, limit: int = 300, budget: float = 4.0) -> tuple[list[Path], Path | None]:
    """Files whose name contains every word of `query`, and the best matching
    folder (for "play my <folder> folder"). Common media places first."""
    words = _words(query)
    exts = _media_ext()
    files: list[Path] = []
    best_dir, best_dir_score = None, 0
    seen_files: set[str] = set()
    t0 = time.time()
    for root in _roots():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth > 6:
                dirnames[:] = []
            name = Path(dirpath).name.lower()
            if words and all(w in name for w in words):
                n_media = sum(1 for f in filenames if Path(f).suffix.lower() in exts)
                score = n_media + (50 if name == " ".join(words) else 0)
                if n_media and score > best_dir_score:
                    best_dir, best_dir_score = Path(dirpath), score
            for f in filenames:
                if Path(f).suffix.lower() in exts and (not words or all(w in f.lower() for w in words)):
                    fp = Path(dirpath) / f
                    key = str(fp).lower()
                    if key in seen_files:
                        continue
                    seen_files.add(key)
                    files.append(fp)
                    if len(files) >= limit:
                        return files, best_dir
            if time.time() - t0 > budget:
                return files, best_dir
    return files, best_dir


def media_player(parameters: dict, player=None) -> str:
    p = parameters or {}
    action = str(p.get("action", "")).lower().strip()
    if player is None or not hasattr(player, "media"):
        return "The media player is only available in the Habibi window."
    if action in ("play", "add"):
        path = str(p.get("path", "") or "").strip().strip('"')
        query = str(p.get("query", "") or "").strip()
        targets: list[str] = []
        if path:
            pp = Path(os.path.expandvars(path)).expanduser()
            if not pp.exists():
                return f"'{path}' doesn't exist."
            targets = [str(pp)]
        elif query:
            if query.lower() in ("music", "my music", "songs", "my songs"):
                targets = [str(Path.home() / "Music")]
            elif query.lower() in ("videos", "my videos", "movies"):
                targets = [str(Path.home() / "Videos")]
            else:
                files, folder = search(query)
                if folder is not None and (not files or len(files) < 3 or
                                           all(f.parent == folder for f in files)):
                    targets = [str(folder)]
                elif files:
                    targets = [str(f) for f in sorted(files, key=lambda f: str(f).lower())]
                elif folder is not None:
                    targets = [str(folder)]
                else:
                    return (f"I couldn't find any audio or video matching '{query}' in Music, "
                            f"Videos, Downloads, Desktop, Documents or other drives.")
        else:
            return player.media("resume")
        for t in targets:
            b = safety.blocked_path(t)
            if b:
                return f"'{b}' is a blocked folder, so I won't open files from it."
        return player.media("play", paths=targets, append=(action == "add"))
    if action in ("pause", "resume", "toggle", "stop", "next", "previous", "status", "list"):
        return player.media(action)
    if action == "seek":
        return player.media("seek", seconds=float(p.get("seconds", 0)), relative=bool(p.get("relative", False)))
    if action == "volume":
        v = p.get("volume")
        return player.media("volume", volume=None if v is None else int(v), relative=bool(p.get("relative", False)))
    if action == "shuffle":
        return player.media("shuffle", on=bool(p.get("on", True)))
    if action == "repeat":
        return player.media("repeat", mode=str(p.get("mode", "all")))
    if action == "rgb":
        return player.media("rgb", on=bool(p.get("on", True)))
    if action == "play_index":
        return player.media("play_index", index=int(p.get("index", 1)))
    return "Unknown action. Use play, add, pause, resume, stop, next, previous, seek, volume, shuffle, repeat, rgb, list, play_index or status."


PLUGIN = {
    "name": "media_player",
    "description": (
        "Habibi's BUILT-IN audio/video player (left sidebar, plays inside Habibi, no browser). Use it "
        "to play music, songs, videos or movies FROM THEIR PC, and to pause/next/seek/volume whatever "
        "the player is playing (also YouTube started by the youtube_player plugin). "
        "play(query) searches Music/Videos/Downloads/Desktop/Documents and other drives by file or "
        "folder name ('my music' = the Music folder); play(path) plays an exact file or folder; "
        "add = append to the playlist. Then pause, resume, stop, next, previous, seek(seconds, "
        "relative), volume(volume 0-100, relative), shuffle(on), repeat(mode off|all|one), "
        "rgb(on) for the music-reactive colours, list, play_index(index), status."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "play | add | pause | resume | stop | next | previous | seek | volume | shuffle | repeat | rgb | list | play_index | status"},
            "query": {"type": "STRING", "description": "play/add: words from the file or folder name, e.g. 'arijit', 'interstellar', 'my music'"},
            "path": {"type": "STRING", "description": "play/add: exact file or folder path"},
            "seconds": {"type": "NUMBER", "description": "seek: position in seconds, or the jump if relative (negative = back)"},
            "relative": {"type": "BOOLEAN", "description": "seek/volume: change by this amount instead of setting it"},
            "volume": {"type": "INTEGER", "description": "volume: 0-100 (or the change if relative)"},
            "on": {"type": "BOOLEAN", "description": "shuffle/rgb: on or off"},
            "mode": {"type": "STRING", "description": "repeat: off | all | one"},
            "index": {"type": "INTEGER", "description": "play_index: track number from list (1 = first)"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Plugin entry point (switch this tool off in ⚙ PLUGINS)."""
    return media_player(parameters or {}, player=player)
