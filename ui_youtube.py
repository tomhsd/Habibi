"""
ui_youtube.py — YouTube inside Habibi's media player.

Uses YouTube's official embedded player (IFrame Player API) in a built-in web
view, so videos and playlists play inside Habibi's window — no separate
browser — while staying within YouTube's terms (no downloading, ads and
creator settings respected). Videos whose owner disabled embedding can't play
here; Habibi says so and skips them in a playlist.

Controlled by the same sidebar card and voice commands as local files
(ui_media.MediaEngine delegates to YouTubePlayer while mode == "youtube").
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal

_ORIGIN = "https://habibi.local"

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
        from PyQt6.QtWebEngineCore import QWebEngineSettings
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        self.view = QWebEngineView()
        st = self.view.settings()
        st.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        st.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
        self.view.page().fullScreenRequested.connect(lambda r: r.accept())
        self.s: dict = {}
        self._loaded = False
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
            self.view.setHtml(_HTML, QUrl(_ORIGIN + "/"))

    def load(self, target: dict) -> None:
        self._ensure_page()
        o = {k: target[k] for k in ("id", "list", "index") if k in target}
        o["vol"] = self._eff_vol()
        self._last_err = None
        self.s = {"ready": self.s.get("ready", False), "state": 3, "title": "Loading from YouTube…"}
        self.view.page().runJavaScript(f"load({json.dumps(o)})")
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
        if self._loaded:
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
        import threading
        threading.Thread(target=_run, daemon=True).start()

    def playing(self) -> bool:
        return self.s.get("state") in (1, 3)

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
