"""
actions/find_files.py — find any file on the PC in a moment.

Uses the Windows Search index (the same one the Start menu uses), so it is
instant and can also search inside documents. If the index isn't available it
falls back to scanning your user folders for a few seconds.
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from core import automation as A
from core import safety


def _q(s: str) -> str:
    return s.replace("'", "''")


def _index_search(name: str, ext: str, content: str, folder: str, days: int, limit: int) -> list[tuple]:
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    conds = ["System.ItemType <> 'Directory'"]
    if name:
        conds.append(f"System.FileName LIKE '%{_q(name)}%'")
    if ext:
        e = ext if ext.startswith(".") else "." + ext
        conds.append(f"System.FileExtension = '{_q(e.lower())}'")
    if content:
        conds.append(f"FREETEXT('{_q(content)}')")
    if folder:
        conds.append(f"SCOPE='file:{_q(str(Path(folder).expanduser()))}'")
    if days:
        since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        conds.append(f"System.DateModified >= '{since}'")
    sql = (f"SELECT TOP {limit} System.ItemPathDisplay, System.DateModified, System.Size "
           f"FROM SystemIndex WHERE {' AND '.join(conds)} ORDER BY System.DateModified DESC")
    conn = win32com.client.Dispatch("ADODB.Connection")
    conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
    rs = win32com.client.Dispatch("ADODB.Recordset")
    rs.Open(sql, conn)
    out = []
    while not rs.EOF:
        out.append((rs.Fields.Item("System.ItemPathDisplay").Value,
                    rs.Fields.Item("System.DateModified").Value,
                    rs.Fields.Item("System.Size").Value or 0))
        rs.MoveNext()
    rs.Close()
    conn.Close()
    return out


def _walk_search(name: str, ext: str, folder: str, days: int, limit: int) -> list[tuple]:
    roots = [Path(folder).expanduser()] if folder else [
        Path.home() / d for d in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")]
    e = ("." + ext.lstrip(".")).lower() if ext else ""
    since = time.time() - days * 86400 if days else 0
    deadline = time.time() + 8
    out = []
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "AppData")]
            for f in files:
                if name and name.lower() not in f.lower():
                    continue
                if e and not f.lower().endswith(e):
                    continue
                p = Path(dirpath) / f
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime < since:
                    continue
                out.append((str(p), datetime.fromtimestamp(st.st_mtime), st.st_size))
            if time.time() > deadline or len(out) >= limit * 4:
                break
    out.sort(key=lambda r: r[1], reverse=True)
    return out[:limit]


def _fmt(rows: list[tuple]) -> str:
    def size(n):
        n = float(n or 0)
        for u in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {u}"
            n /= 1024
        return f"{n:.1f} TB"
    lines = []
    for i, (path, mod, sz) in enumerate(rows, 1):
        try:
            when = mod.strftime("%d %b %Y %H:%M") if hasattr(mod, "strftime") else str(mod)[:16]
        except Exception:
            when = ""
        lines.append(f"{i}. {path}  ({size(sz)}, {when})")
    return "\n".join(lines)


def find_files(parameters: dict, player=None) -> str:
    err = A.windows_only()
    if err:
        return err
    action = str(parameters.get("action", "search"))
    path = str(parameters.get("path", "") or "")
    if action in ("open", "reveal"):
        if not path or not Path(path).exists():
            return "That file doesn't exist. Search for it first."
        b = safety.blocked_path(path)
        if b:
            return f"'{b}' is a blocked folder."
        if action == "open":
            os.startfile(path)
            return f"Opened {Path(path).name}."
        subprocess.Popen(["explorer", "/select,", str(Path(path))])
        return f"Showing {Path(path).name} in File Explorer."

    name = str(parameters.get("name", "") or "")
    ext = str(parameters.get("extension", "") or "")
    content = str(parameters.get("content", "") or "")
    folder = str(parameters.get("folder", "") or "")
    days = int(parameters.get("days", 0) or (7 if action == "recent" else 0))
    limit = max(1, min(40, int(parameters.get("limit", 15) or 15)))
    if not (name or ext or content or days):
        return "Tell me part of the file name, a type (pdf, docx…), words inside it, or how recent."
    try:
        rows = _index_search(name, ext, content, folder, days, limit * 2)
        how = "Windows Search"
    except Exception:
        rows = _walk_search(name, ext, folder, days, limit * 2)
        how = "a folder scan"
    rows = [r for r in rows if not safety.blocked_path(r[0])][:limit]
    if not rows:
        return f"No files found (searched with {how})."
    return f"Found {len(rows)} file(s):\n" + _fmt(rows) + "\nSay 'open number N' and I'll open it (action open with its path)."


TOOL = {
    "name": "find_files",
    "description": (
        "Find files anywhere on the PC instantly (Windows Search index). search(name, extension, "
        "content=words inside the file, folder, days=modified in the last N days, limit); "
        "recent(days, extension) — recently changed files; open(path); reveal(path) — show it "
        "in File Explorer. Prefer this over file_controller for finding files."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "search | recent | open | reveal"},
            "name": {"type": "STRING", "description": "Part of the file name"},
            "extension": {"type": "STRING", "description": "pdf, docx, xlsx, jpg, mp4…"},
            "content": {"type": "STRING", "description": "Words inside the document"},
            "folder": {"type": "STRING", "description": "Only inside this folder"},
            "days": {"type": "INTEGER", "description": "Modified in the last N days"},
            "limit": {"type": "INTEGER", "description": "Max results (default 15)"},
            "path": {"type": "STRING", "description": "open/reveal: full path from a search result"},
        },
        "required": ["action"],
    },
    "handler": find_files,
}
