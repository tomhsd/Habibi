"""
plugins/office.py — control Excel, Word and PowerPoint directly (no clicking).

Talks to Office through its own automation interface (COM), the same way
macros do, so every cell, paragraph and slide is changed exactly. Works with
the document that is already open, or opens one by path. Needs Microsoft
Office installed on the PC.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from core import automation as A
from core import safety

_APPS = {"excel": "Excel.Application", "word": "Word.Application", "powerpoint": "PowerPoint.Application"}


def _app(kind: str):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    prog = _APPS[kind]
    try:
        app = win32com.client.GetActiveObject(prog)
    except Exception:
        app = win32com.client.Dispatch(prog)
    try:
        app.Visible = True
    except Exception:
        pass
    return app


def _path(p: str) -> str:
    if not p:
        return ""
    q = Path(os.path.expandvars(p)).expanduser()
    if not q.is_absolute():
        q = Path.home() / "Documents" / q
    b = safety.blocked_path(q)
    if b:
        raise PermissionError(f"'{b}' is a blocked folder.")
    return str(q)


# ── Excel ────────────────────────────────────────────────────────────────────

def _excel(action: str, p: dict) -> str:
    xl = _app("excel")
    if action == "open":
        wb = xl.Workbooks.Open(_path(p["path"]))
        return f"Opened {wb.Name}. Sheets: {', '.join(s.Name for s in wb.Worksheets)}"
    if action == "new":
        wb = xl.Workbooks.Add()
        return f"New workbook {wb.Name} created."
    wb = xl.ActiveWorkbook
    if wb is None:
        return "No Excel workbook is open. Use action open with a path, or new."
    ws = wb.Worksheets(p["sheet"]) if p.get("sheet") else wb.ActiveSheet
    rng = p.get("range") or ""
    if action == "read":
        r = ws.Range(rng) if rng else ws.UsedRange
        vals = r.Value
        if not isinstance(vals, tuple):
            vals = ((vals,),)
        rows = [" | ".join("" if v is None else str(v) for v in row) for row in vals[:60]]
        return f"{ws.Name}!{r.Address}:\n" + "\n".join(rows)[:3500]
    if action == "write":
        if not rng:
            return "Which cells? e.g. range 'B2' or 'A1:C3'."
        val = p.get("value", "")
        try:
            parsed = json.loads(val) if isinstance(val, str) and val.strip().startswith("[") else val
        except Exception:
            parsed = val

        def _do() -> str:
            r = ws.Range(rng)
            if isinstance(parsed, list):
                data = [row if isinstance(row, list) else [row] for row in parsed]
                r = ws.Range(r.Cells(1, 1), r.Cells(len(data), max(len(x) for x in data)))
                r.Value = data
            else:
                r.Value = parsed
            return f"Wrote to {ws.Name}!{r.Address}."
        return safety.guarded("change", f"Write to Excel {rng}", str(val)[:80], _do)
    if action == "formula":
        if not rng:
            return "Which cell gets the formula?"

        def _do() -> str:
            c = ws.Range(rng)
            c.Formula = p.get("value", "")
            return f"{ws.Name}!{c.Address} = {c.Formula} → {c.Value}"
        return safety.guarded("change", f"Formula in {rng}", str(p.get("value", "")), _do)
    if action == "save":
        def _do() -> str:
            if p.get("path"):
                wb.SaveAs(_path(p["path"]))
            else:
                wb.Save()
            return f"Saved {wb.Name}."
        return safety.guarded("change", f"Save {wb.Name}", "", _do)
    if action == "export_pdf":
        out = _path(p.get("path") or str(Path(wb.FullName).with_suffix(".pdf")))
        wb.ExportAsFixedFormat(0, out)
        return f"Exported PDF: {out}"
    return "Excel actions: open, new, read, write, formula, save, export_pdf."


# ── Word ─────────────────────────────────────────────────────────────────────

def _word(action: str, p: dict) -> str:
    wd = _app("word")
    if action == "open":
        d = wd.Documents.Open(_path(p["path"]))
        return f"Opened {d.Name} ({d.Paragraphs.Count} paragraphs)."
    if action == "new":
        d = wd.Documents.Add()
        return f"New document {d.Name} created."
    if wd.Documents.Count == 0:
        return "No Word document is open. Use action open with a path, or new."
    d = wd.ActiveDocument
    if action == "read":
        return f"{d.Name}:\n" + d.Content.Text[:3500]
    if action == "append":
        def _do() -> str:
            d.Content.InsertAfter("\r" + str(p.get("value", "")))
            return f"Added text to {d.Name}."
        return safety.guarded("change", f"Add text to {d.Name}", str(p.get("value", ""))[:80], _do)
    if action == "replace":
        find, rep = str(p.get("find", "")), str(p.get("value", ""))
        if not find:
            return "What text should I replace? (find)"

        def _do() -> str:
            rng = d.Content
            f = rng.Find
            f.ClearFormatting()
            f.Replacement.ClearFormatting()
            ok = f.Execute(find, False, False, False, False, False, True, 1, False, rep, 2)
            return f"Replaced '{find}' with '{rep}'." if ok else f"'{find}' was not found."
        return safety.guarded("change", f"Replace in {d.Name}", f"{find} → {rep}", _do)
    if action == "save":
        def _do() -> str:
            if p.get("path"):
                d.SaveAs2(_path(p["path"]))
            else:
                d.Save()
            return f"Saved {d.Name}."
        return safety.guarded("change", f"Save {d.Name}", "", _do)
    if action == "export_pdf":
        out = _path(p.get("path") or str(Path(d.FullName).with_suffix(".pdf")))
        d.ExportAsFixedFormat(out, 17)
        return f"Exported PDF: {out}"
    return "Word actions: open, new, read, append, replace, save, export_pdf."


# ── PowerPoint ───────────────────────────────────────────────────────────────

def _ppt(action: str, p: dict) -> str:
    pp = _app("powerpoint")
    if action == "open":
        pres = pp.Presentations.Open(_path(p["path"]))
        return f"Opened {pres.Name} ({pres.Slides.Count} slides)."
    if action == "new":
        pres = pp.Presentations.Add()
        return f"New presentation {pres.Name} created."
    if pp.Presentations.Count == 0:
        return "No presentation is open. Use action open with a path, or new."
    pres = pp.ActivePresentation
    if action == "read":
        out = []
        for i in range(1, pres.Slides.Count + 1):
            texts = []
            for sh in pres.Slides(i).Shapes:
                try:
                    if sh.HasTextFrame and sh.TextFrame.HasText:
                        texts.append(sh.TextFrame.TextRange.Text.replace("\r", " / "))
                except Exception:
                    pass
            out.append(f"Slide {i}: " + " | ".join(texts))
        return "\n".join(out)[:3500]
    if action == "add_slide":
        def _do() -> str:
            idx = pres.Slides.Count + 1
            layout = pres.SlideMaster.CustomLayouts(2 if pres.SlideMaster.CustomLayouts.Count >= 2 else 1)
            s = pres.Slides.AddSlide(idx, layout)
            if s.Shapes.Count >= 1:
                s.Shapes(1).TextFrame.TextRange.Text = str(p.get("title", ""))
            if s.Shapes.Count >= 2:
                s.Shapes(2).TextFrame.TextRange.Text = str(p.get("value", ""))
            return f"Added slide {idx} to {pres.Name}."
        return safety.guarded("change", f"Add slide to {pres.Name}", str(p.get("title", "")), _do)
    if action == "save":
        def _do() -> str:
            if p.get("path"):
                pres.SaveAs(_path(p["path"]))
            else:
                pres.Save()
            return f"Saved {pres.Name}."
        return safety.guarded("change", f"Save {pres.Name}", "", _do)
    if action == "export_pdf":
        out = _path(p.get("path") or str(Path(pres.FullName).with_suffix(".pdf")))
        pres.SaveAs(out, 32)
        return f"Exported PDF: {out}"
    return "PowerPoint actions: open, new, read, add_slide, save, export_pdf."


def office(parameters: dict, player=None) -> str:
    err = A.windows_only()
    if err:
        return err
    app = str(parameters.get("app", "")).lower().replace(" ", "")
    action = str(parameters.get("action", "")).lower()
    try:
        if app == "excel":
            return _excel(action, parameters)
        if app == "word":
            return _word(action, parameters)
        if app in ("powerpoint", "ppt"):
            return _ppt(action, parameters)
        return "app must be excel, word or powerpoint."
    except PermissionError as e:
        return str(e)
    except KeyError as e:
        return f"Missing {e} for this action."
    except Exception as e:
        msg = str(e)
        if "Invalid class string" in msg or "-2147221005" in msg:
            return f"Microsoft {app.title()} doesn't seem to be installed on this PC."
        return f"{app.title()} error: {msg[:200]}"


PLUGIN = {
    "name": "office",
    "description": (
        "Control Microsoft Excel, Word and PowerPoint DIRECTLY (exact, no clicking). Works on the "
        "open document or opens one by path. Excel: open(path), new, read(range, sheet), "
        "write(range, value — a value or JSON rows like [[1,2],[3,4]]), formula(range=cell, "
        "value='=SUM(B2:B20)'), save(path?), export_pdf. Word: open, new, read, append(value), "
        "replace(find, value), save, export_pdf. PowerPoint: open, new, read, add_slide(title, "
        "value=body text), save, export_pdf. Relative paths are inside Documents."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app": {"type": "STRING", "description": "excel | word | powerpoint"},
            "action": {"type": "STRING", "description": "open, new, read, write, formula, append, replace, add_slide, save, export_pdf"},
            "path": {"type": "STRING", "description": "File path (open / save as / pdf)"},
            "range": {"type": "STRING", "description": "Excel cells, e.g. 'B2' or 'A1:D20'"},
            "sheet": {"type": "STRING", "description": "Excel sheet name (default: active)"},
            "value": {"type": "STRING", "description": "Value / formula / text"},
            "find": {"type": "STRING", "description": "Word replace: text to find"},
            "title": {"type": "STRING", "description": "PowerPoint slide title"},
        },
        "required": ["app", "action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Plugin entry point (switch this tool off in ⚙ PLUGINS)."""
    return office(parameters or {}, player=player)
