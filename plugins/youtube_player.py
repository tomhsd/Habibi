"""
plugins/youtube_player.py — YouTube videos and playlists inside Habibi's media player.

A drop-in plugin (switch it off in ⚙ PLUGINS). It uses YouTube's official
embedded player (IFrame Player API) in a built-in web view, so videos and
playlists play inside Habibi — no separate browser — within YouTube's terms:
no downloading and no ad blocking. Sign in to YouTube once (▶ YT → Sign in);
with YouTube Premium, videos play without ads. The login is remembered in
config/youtube_profile/ on this PC only.

UI: setup_ui() adds a ▶ YT button to the MEDIA PLAYER card and a view to its
video page; the card's play/pause/next/seek/volume buttons, the playlist and
the RGB colours all work with it (ui_media external-source API).
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal

_ORIGIN = "https://www.youtube.com"
_SIGN_IN_URL = ("https://accounts.google.com/ServiceLogin?service=youtube&hl=en"
                "&continue=https%3A%2F%2Fwww.youtube.com%2F")


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="referrer" content="strict-origin-when-cross-origin">
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}
#p{position:absolute;inset:0;width:100%;height:100%}</style></head>
<body><div id="p"></div>
<script>
var player=null, ready=false, err=null, pending=null, apiLoaded=false;
function onYouTubeIframeAPIReady(){
  apiLoaded=true;
  player=new YT.Player('p',{width:'100%',height:'100%',
    playerVars:{autoplay:1,playsinline:1,rel:0,modestbranding:1,enablejsapi:1,origin:location.origin},
    events:{onReady:function(){ready=true; if(pending){run(pending);pending=null;}},
            onError:function(e){err=e.data;}}});
}
function load(o){ err=null; if(!ready){pending=o;return;} run(o); }
function run(o){
  if(o.list){ player.loadPlaylist({list:o.list,listType:'playlist',index:o.index||0}); }
  else { player.loadVideoById(o.id); }
  if(o.vol!==undefined){ player.setVolume(o.vol); }
}
function call(name,a,b){ if(ready && player[name]) return player[name](a,b); }
function state(){
  if(!ready) return JSON.stringify({ready:false, api:apiLoaded});
  var d=player.getVideoData()||{};
  return JSON.stringify({ready:true,state:player.getPlayerState(),pos:player.getCurrentTime()||0,
    dur:player.getDuration()||0,title:d.title||'',author:d.author||'',id:d.video_id||'',
    idx:player.getPlaylistIndex(),list:player.getPlaylist()||[],vol:player.getVolume(),err:err});
}
</script>
<script src="https://www.youtube.com/iframe_api"></script>
</body></html>"""

_server = None


def _serve_page() -> str:
    """Start (once) a tiny web server on localhost that serves the player page,
    and return its address. Only this machine can reach it."""
    global _server
    if _server is None:
        import http.server
        import threading

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = _HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        _server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=_server.serve_forever, daemon=True).start()
    return f"http://localhost:{_server.server_address[1]}/"


_ERRORS = {
    2: "That YouTube link isn't valid.",
    5: "This video can't play in Habibi's player.",
    100: "That video was removed or is private.",
    101: "The owner doesn't allow this video to play outside YouTube.",
    150: "The owner doesn't allow this video to play outside YouTube.",
    153: "YouTube refused the embedded player (missing referrer).",
}


# ── links ────────────────────────────────────────────────────────────────────

def parse_link(text: str) -> dict:
    """{'id': video id} and/or {'list': playlist id} from a URL or bare id."""
    t = (text or "").strip()
    out: dict = {}
    if not t:
        return out
    if re.fullmatch(r"(PL|OL|UU|LL|FL|RD)[A-Za-z0-9_-]{10,}", t):
        return {"list": t}
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", t):
        return {"id": t}
    try:
        u = urlparse(t if "://" in t else "https://" + t)
    except Exception:
        return out
    if not re.search(r"(youtube\.com|youtu\.be|youtube-nocookie\.com)$", u.netloc.split(":")[0]):
        return out
    q = parse_qs(u.query)
    if q.get("list"):
        out["list"] = q["list"][0]
    if q.get("v"):
        out["id"] = q["v"][0][:11]
    m = re.search(r"(?:youtu\.be/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{11})", t)
    if m and "id" not in out:
        out["id"] = m.group(1)
    if q.get("index") and "list" in out:
        try:
            out["index"] = max(0, int(q["index"][0]) - 1)
        except ValueError:
            pass
    return out


def search(query: str, playlist: bool = False) -> dict:
    """First matching video (or playlist) on YouTube's public search page."""
    import requests
    from urllib.parse import quote_plus
    sp = "EgIQAw%3D%3D" if playlist else "EgIQAQ%3D%3D"
    r = requests.get(f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={sp}",
                     headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
                              "Accept-Language": "en-US,en;q=0.9"}, timeout=10)
    html = r.text
    if playlist:
        for pid in re.findall(r'"playlistId":"((?:PL|OL)[A-Za-z0-9_-]{10,})"', html):
            return {"list": pid}
        return {}
    for vid in re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html):
        if f"/shorts/{vid}" not in html:
            return {"id": vid}
    return {}


# ── player ───────────────────────────────────────────────────────────────────

class YouTubePlayer(QObject):
    titles_ready = pyqtSignal()
    changed = pyqtSignal()
    position = pyqtSignal(int, int)
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        self.name, self.tag = "YouTube", "YT"
        # A saved profile keeps your YouTube sign-in (Premium = no ads) between runs
        store = _base_dir() / "config" / "youtube_profile"
        store.mkdir(parents=True, exist_ok=True)
        self.profile = QWebEngineProfile("habibi-youtube", self)
        self.profile.setPersistentStoragePath(str(store / "storage"))
        self.profile.setCachePath(str(store / "cache"))
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        self.view = QWebEngineView()
        self.view.setPage(QWebEnginePage(self.profile, self.view))
        st = self.view.settings()
        st.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        st.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
        self.view.page().fullScreenRequested.connect(lambda r: r.accept())
        self.s: dict = {}
        self._loaded = False
        self._page_ready = False
        self._queued: str | None = None      # JS to run once the page has loaded
        self.view.loadFinished.connect(self._on_page_loaded)
        self._vol = 70
        self._duck = False
        self._last_key = None
        self._last_err = None
        self.titles: dict[str, str] = {}
        self._fetching: set[str] = set()
        self.titles_ready.connect(self.changed.emit)
        self._t = QTimer(self)
        self._t.timeout.connect(self._poll)
        self._t.start(400)

    # ── loading ──────────────────────────────────────────────────────────────
    def _ensure_page(self) -> None:
        if not self._loaded:
            self._loaded = True
            self._page_ready = False
            # Served from a tiny local web server: YouTube refuses embeds (error
            # 152/153) from a page that has no real http(s) origin or Referer.
            self.view.load(QUrl(_serve_page()))

    def _on_page_loaded(self, ok: bool) -> None:
        self._page_ready = True
        if self._queued:
            js, self._queued = self._queued, None
            self.view.page().runJavaScript(js)

    def _run_js(self, js: str) -> None:
        """Run now, or as soon as the player page has loaded (the first load
        used to be lost because load() didn't exist yet)."""
        if self._page_ready:
            self.view.page().runJavaScript(js)
        else:
            self._queued = js

    def load(self, target: dict) -> None:
        self._ensure_page()
        o = {k: target[k] for k in ("id", "list", "index") if k in target}
        o["vol"] = self._eff_vol()
        self._last_err = None
        self.s = {"ready": self.s.get("ready", False), "state": 3, "title": "Loading from YouTube…"}
        self._run_js(f"load({json.dumps(o)})")
        self.changed.emit()

    # ── controls ─────────────────────────────────────────────────────────────
    def _js(self, name: str, *args) -> None:
        a = ",".join(json.dumps(x) for x in args)
        self.view.page().runJavaScript(f"call({json.dumps(name)}{',' if a else ''}{a})")

    def toggle(self) -> None:
        self._js("pauseVideo" if self.playing() else "playVideo")

    def play(self) -> None:
        self._js("playVideo")

    def pause(self) -> None:
        self._js("pauseVideo")
        self.s["state"] = 2

    def stop(self) -> None:
        self._js("stopVideo")
        self.s["state"] = 5

    def next(self) -> bool:
        if len(self.s.get("list") or []) > 1:
            self._js("nextVideo")
            return True
        return False

    def previous(self) -> bool:
        if self.s.get("pos", 0) > 4 or len(self.s.get("list") or []) <= 1:
            self._js("seekTo", 0, True)
        else:
            self._js("previousVideo")
        return True

    def play_index(self, i: int) -> None:
        self._js("playVideoAt", int(i))

    def seek(self, ms: int) -> None:
        self._js("seekTo", max(0.0, ms / 1000.0), True)

    def set_volume(self, pct: int) -> None:
        self._vol = max(0, min(100, int(pct)))
        self._js("setVolume", self._eff_vol())

    def duck(self, on: bool) -> None:
        if on != self._duck:
            self._duck = on
            self._js("setVolume", self._eff_vol())

    def _eff_vol(self) -> int:
        return int(self._vol * (0.25 if self._duck else 1.0))

    # ── state ────────────────────────────────────────────────────────────────
    def _poll(self) -> None:
        if self._loaded and self._page_ready:
            self.view.page().runJavaScript("state()", self._got)

    def _got(self, raw) -> None:
        try:
            s = json.loads(raw) if raw else {}
        except Exception:
            return
        if not s.get("ready"):
            return
        self.s = s
        self.position.emit(int(s.get("pos", 0) * 1000), int(s.get("dur", 0) * 1000))
        key = (s.get("state"), s.get("id"), s.get("idx"), s.get("title"), len(s.get("list") or []))
        if key != self._last_key:
            self._last_key = key
            self.changed.emit()
        e = s.get("err")
        if e is not None and e != self._last_err:
            self._last_err = e
            self.error.emit(_ERRORS.get(int(e), f"YouTube error {e}."))
            if len(s.get("list") or []) > 1:            # skip what can't play
                QTimer.singleShot(1500, lambda: self._js("nextVideo"))

    def fetch_titles(self, ids: list[str]) -> None:
        """Playlist titles from YouTube's public oEmbed endpoint, in the background."""
        todo = [v for v in ids[:200] if v not in self.titles and v not in self._fetching]
        if not todo:
            return
        self._fetching.update(todo)

        def _run():
            import requests
            got = False
            for v in todo:
                try:
                    r = requests.get("https://www.youtube.com/oembed",
                                     params={"url": f"https://www.youtube.com/watch?v={v}", "format": "json"},
                                     timeout=6)
                    if r.ok:
                        self.titles[v] = r.json().get("title", v)
                        got = True
                except Exception:
                    pass
            if got:
                self.titles_ready.emit()
        threading.Thread(target=_run, daemon=True).start()

    def playing(self) -> bool:
        return self.s.get("state") in (1, 3)

    def loading(self) -> bool:
        return self.s.get("state") == 3 or not self.s.get("title")

    def position_ms(self) -> int:
        return int(self.s.get("pos", 0) * 1000)

    def item_titles(self) -> list[str]:
        ids = list(self.s.get("list") or [])
        if ids:
            self.fetch_titles(ids)
            return [self.titles.get(v, v) for v in ids]
        return [self.title()] if self.title() else []

    def link(self) -> str:
        v = self.s.get("id", "")
        return f"https://www.youtube.com/watch?v={v}" if v else "https://www.youtube.com/"

    # ── sign-in (YouTube Premium → no ads) ───────────────────────────────────
    def sign_in(self, parent=None) -> None:
        """A small window with Google's own sign-in page, using the same saved
        profile as the player. Habibi never sees your password."""
        from PyQt6.QtWebEngineCore import QWebEnginePage
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout
        dlg = QDialog(parent)
        dlg.setWindowTitle("Sign in to YouTube")
        dlg.resize(520, 720)
        lay = QVBoxLayout(dlg)
        info = QLabel("Sign in with your Google account. With YouTube Premium, videos in "
                      "Habibi play without ads. Close this window when you're done.")
        info.setWordWrap(True)
        lay.addWidget(info)
        web = QWebEngineView(dlg)
        web.setPage(QWebEnginePage(self.profile, web))
        web.load(QUrl(_SIGN_IN_URL))
        lay.addWidget(web, 1)
        done = QPushButton("DONE")
        done.clicked.connect(dlg.accept)
        lay.addWidget(done)
        dlg.finished.connect(self._after_sign_in)
        dlg.show()
        self._sign_in_dlg = dlg

    def _after_sign_in(self, *_):
        if self._loaded:                 # reload so the player picks up the login
            self._loaded = False
            self._ensure_page()
        self.changed.emit()

    def sign_out(self) -> None:
        self.profile.cookieStore().deleteAllCookies()
        self.profile.clearHttpCache()
        if self._loaded:
            self._loaded = False
            self._ensure_page()

    def paused(self) -> bool:
        return self.s.get("state") == 2

    def title(self) -> str:
        t = self.s.get("title") or ""
        a = self.s.get("author") or ""
        return f"{t} — {a}" if t and a else t

    def count(self) -> int:
        return max(1, len(self.s.get("list") or []))

    def index(self) -> int:
        i = self.s.get("idx")
        return int(i) if isinstance(i, int) and i >= 0 else 0

    def status_text(self) -> str:
        st = "Playing" if self.playing() else "Paused" if self.paused() else "Stopped"
        pos, dur = int(self.s.get("pos", 0)), int(self.s.get("dur", 0))
        pl = f", playlist video {self.index() + 1} of {self.count()}" if self.count() > 1 else ""
        return (f"{st} on YouTube: {self.title() or 'loading…'}{pl}, "
                f"{pos // 60}:{pos % 60:02d} / {dur // 60}:{dur % 60:02d}, volume {self._vol}%.")


# ══════════════════════════════════════════════════════════════════════════════
#  Plugin glue
# ══════════════════════════════════════════════════════════════════════════════

_yt: YouTubePlayer | None = None


def setup_ui(player) -> None:
    """Runs once on the Qt thread: attach YouTube to the built-in media player."""
    global _yt
    engine = getattr(player, "media_engine", None)
    card = getattr(player, "media_card", None)
    if engine is None or card is None or _yt is not None:
        return
    _yt = YouTubePlayer(engine)
    player.add_media_view(_yt.view)
    engine.attach_source(_yt)

    from PyQt6.QtWidgets import QInputDialog, QMenu
    btn = card.add_source_button("▶ YOUTUBE", "YouTube: play a link or search, sign in (Premium = no ads)",
                                 lambda: None)
    menu = QMenu(btn)

    def _ask():
        text, ok = QInputDialog.getText(card, "YouTube", "Paste a YouTube video or playlist link, "
                                                         "or type what to search for:")
        text = (text or "").strip()
        if not ok or not text:
            return
        target = parse_link(text)
        if target:
            engine.play_external(target)
            return
        card.now.setText("Searching YouTube…")

        def _search():                       # network off the Qt thread
            try:
                t = search(text, playlist="playlist" in text.lower())
            except Exception:
                t = {}
            player.run_in_ui(lambda: engine.play_external(t) if t else
                             card.now.setText("Nothing found on YouTube."))
        threading.Thread(target=_search, daemon=True).start()

    menu.addAction("▶  Play a link or search…", _ask)
    menu.addAction("👤  Sign in to YouTube (Premium = no ads)", lambda: _yt.sign_in(card.window()))
    menu.addAction("⎋  Sign out of YouTube", _yt.sign_out)
    btn.setMenu(menu)


PLUGIN = {
    "name": "youtube_player",
    "description": (
        "Play YouTube videos or playlists INSIDE Habibi's media player (left sidebar), not in a "
        "browser. Use for 'play X on YouTube', a YouTube link, 'play a lofi playlist on YouTube'. "
        "action=play with link (video/playlist URL) or query (+ playlist=true to find a playlist); "
        "action=sign_in opens Google's sign-in so a YouTube Premium account plays without ads; "
        "action=sign_out. Pause/next/seek/volume afterwards → media_player. It cannot download "
        "videos or block ads (YouTube's terms)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "play | sign_in | sign_out"},
            "link": {"type": "STRING", "description": "play: a YouTube video or playlist URL"},
            "query": {"type": "STRING", "description": "play: what to search for on YouTube"},
            "playlist": {"type": "BOOLEAN", "description": "play with query: find a playlist instead of one video"},
        },
        "required": [],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action", "play") or "play").lower().strip()
    if player is None or not hasattr(player, "run_in_ui"):
        return "YouTube playback needs the Habibi window."
    if _yt is None:
        try:
            player.run_in_ui(lambda: setup_ui(player))
        except Exception as e:
            if isinstance(e, ImportError) and "WebEngine" in str(e):
                import sys
                return ("The YouTube player needs the PyQt6-WebEngine package, which isn't installed. "
                        f"Run this once, then restart Habibi: \"{sys.executable}\" -m pip install PyQt6-WebEngine")
            return f"The YouTube player couldn't start: {e}"
    if _yt is None:
        return "The YouTube player isn't available on this PC (Qt WebEngine missing)."
    engine = player.media_engine
    try:
        if action == "sign_in":
            player.run_in_ui(lambda: _yt.sign_in(player.media_card.window()))
            return ("I opened YouTube's sign-in window. Sign in with your Google account; "
                    "with YouTube Premium, videos here play without ads.")
        if action == "sign_out":
            player.run_in_ui(_yt.sign_out)
            return "Signed out of YouTube in Habibi's player."
        link = str(p.get("link", "") or "").strip()
        query = str(p.get("query", "") or "").strip()
        target = parse_link(link) if link else {}
        if link and not target:
            return "That isn't a YouTube video or playlist link."
        if not target and query:
            target = search(query, playlist=bool(p.get("playlist", False)))
        if not target:
            return "Nothing found on YouTube for that." if query else "Tell me a YouTube link or what to search for."
        player.run_in_ui(lambda: engine.play_external(target))
        kind = "playlist" if target.get("list") else "video"
        if player:
            try:
                player.write_log(f"HABIBI: YouTube {kind} → media player")
            except Exception:
                pass
        return f"Playing the YouTube {kind} in Habibi's media player."
    except Exception as e:
        return f"YouTube failed: {e}"
