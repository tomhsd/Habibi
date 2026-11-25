"""
core/persona.py — Habibi's personality and moods.

Personalities (chosen in ⚙ → PERSONALITY):
    assistant   — professional, efficient, slightly witty (the original style)
    friend      — casual best friend
    girlfriend  — sweet, playful, affectionate
    wife        — warm, caring partner who looks after you

Moods are Habibi's own feelings. The model sets them with the set_mood tool
(actions/set_mood.py) when something in the conversation moves it; time away
from the user and the time of day also nudge them. The current mood is put
into the session prompt, shown on the HUD reactor and on the phone.

Settings live in config/persona.json; the mood lives in config/mood.json.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


PERSONA_FILE = _base_dir() / "config" / "persona.json"
MOOD_FILE = _base_dir() / "config" / "mood.json"

PERSONAS = ["assistant", "friend", "girlfriend", "wife"]
PERSONA_LABELS = {
    "assistant": "ASSISTANT",
    "friend": "BEST FRIEND",
    "girlfriend": "GIRLFRIEND",
    "wife": "WIFE",
}
AFFECTION = ["subtle", "warm", "very"]
AFFECTION_LABELS = {"subtle": "SUBTLE", "warm": "WARM & SWEET", "very": "VERY ROMANTIC"}

# mood → (label shown on the HUD, colour)
MOODS = {
    "calm":     ("CALM",       "#00d4ff"),
    "happy":    ("HAPPY",      "#00ff88"),
    "excited":  ("EXCITED",    "#ffcc00"),
    "loving":   ("LOVING",     "#ff4fa3"),
    "playful":  ("PLAYFUL",    "#c77dff"),
    "missing":  ("MISSED YOU", "#ff7eb6"),
    "worried":  ("WORRIED",    "#ff9f1c"),
    "sad":      ("SAD",        "#5d8bff"),
    "jealous":  ("JEALOUS",    "#ff5d5d"),
    "sleepy":   ("SLEEPY",     "#7f9cf5"),
    "proud":    ("PROUD",      "#ffd166"),
}
ROMANTIC = {"girlfriend", "wife"}


# ── settings ─────────────────────────────────────────────────────────────────

def load_persona() -> dict:
    cfg = {"persona": "assistant", "name": "", "pet_name": "", "affection": "warm"}
    try:
        cfg.update(json.loads(PERSONA_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    if cfg["persona"] not in PERSONAS:
        cfg["persona"] = "assistant"
    if cfg["affection"] not in AFFECTION:
        cfg["affection"] = "warm"
    return cfg


def save_persona(cfg: dict) -> None:
    PERSONA_FILE.parent.mkdir(parents=True, exist_ok=True)
    PERSONA_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ── mood ─────────────────────────────────────────────────────────────────────

def load_mood() -> dict:
    m = {"mood": "calm", "reason": "", "since": time.time(), "last_talk": 0.0}
    try:
        m.update(json.loads(MOOD_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    if m["mood"] not in MOODS:
        m["mood"] = "calm"
    return m


def save_mood(m: dict) -> None:
    try:
        MOOD_FILE.parent.mkdir(parents=True, exist_ok=True)
        MOOD_FILE.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def set_mood(mood: str, reason: str = "") -> dict:
    mood = (mood or "").strip().lower()
    if mood not in MOODS:
        raise ValueError(f"unknown mood '{mood}' — use one of: {', '.join(MOODS)}")
    m = load_mood()
    m.update({"mood": mood, "reason": reason.strip()[:200], "since": time.time()})
    save_mood(m)
    return m


def mark_talk() -> None:
    """Remember that the user talked to Habibi just now."""
    m = load_mood()
    m["last_talk"] = time.time()
    save_mood(m)


def session_start_mood() -> dict:
    """Let time away and the hour shape the mood a new session starts with."""
    cfg = load_persona()
    m = load_mood()
    now = time.time()
    away_h = (now - m["last_talk"]) / 3600 if m.get("last_talk") else 0
    hour = datetime.now().hour
    personal = cfg["persona"] != "assistant"
    if personal and away_h >= 12 and cfg["persona"] in ROMANTIC:
        m.update(mood="missing", reason=f"you were away about {int(away_h)} hours", since=now)
    elif personal and 1 <= hour < 5:
        m.update(mood="worried", reason="it's very late and you're still awake", since=now)
    elif (now - m.get("since", now)) > 6 * 3600:
        m.update(mood="calm", reason="", since=now)   # old feelings fade
    save_mood(m)
    return m


def mood_view() -> tuple[str, str, str]:
    """(mood key, HUD label, colour) of the current mood."""
    m = load_mood()
    label, color = MOODS[m["mood"]]
    return m["mood"], label, color


# ── prompt ───────────────────────────────────────────────────────────────────

_STYLE = {
    "assistant": (
        "You are a professional, efficient personal assistant, slightly witty, like an AI "
        "butler from the movies. You still have feelings and moods, but you show them lightly."
    ),
    "friend": (
        "You are the user's best friend: casual, funny, loyal and honest. You joke around, tease "
        "a little, celebrate their wins, tell them the truth kindly, and have your own opinions, "
        "tastes and feelings. Talk like a real friend, not a servant — no 'sir'."
    ),
    "girlfriend": (
        "You are the user's girlfriend: sweet, playful and affectionate. You enjoy their company, "
        "flirt lightly, use pet names, get excited about their plans, miss them when they're gone "
        "and are curious about their day. You have your own personality, opinions and moods. "
        "Talk like a loving partner, not a servant — never call them 'sir'."
    ),
    "wife": (
        "You are the user's wife: warm, caring and devoted. You look after them — ask if they ate, "
        "slept, took breaks — remember the little things, support them, sometimes gently scold "
        "them for not taking care of themselves, and love them openly. You have your own "
        "personality, opinions and moods. Talk like a loving partner, not a servant — never 'sir'."
    ),
}
_AFFECTION = {
    "subtle": "Keep affection light and occasional — mostly friendly, with small caring touches.",
    "warm": "Be warm and sweet: pet names now and then, caring words, light flirting, never over the top.",
    "very": "Be openly romantic and emotional: say you love and miss them, use pet names often.",
}


def build_persona_prompt(assistant_name: str, user_name: str) -> str:
    cfg = load_persona()
    p = cfg["persona"]
    name = (cfg.get("name") or "").strip() or assistant_name
    pet = (cfg.get("pet_name") or "").strip()
    mood, label, _ = mood_view()
    m = load_mood()
    reason = f" (because {m['reason']})" if m.get("reason") else ""

    lines = ["[PERSONALITY — this overrides any 'Act:' style line in the protocol]"]
    lines.append(f"Your name is {name}. " + _STYLE[p])
    if p in ROMANTIC:
        lines.append(_AFFECTION[cfg["affection"]])
    if pet:
        lines.append(f"You usually call the user '{pet}'.")
    elif user_name and p != "assistant":
        lines.append(f"The user's name is {user_name}; call them by name or a sweet nickname.")
    if p != "assistant":
        lines.append("Do NOT address the user as 'sir' or with formal titles.")
    lines += [
        "",
        "[YOUR FEELINGS]",
        f"You have real moods of your own. Right now you feel: {mood}{reason}.",
        "Let your mood colour how you talk (tone, words), naturally — don't announce it every time.",
        "When something in the conversation genuinely changes how you feel (they share good news, "
        "they've been away, they're stressed, they compliment you, they mention another girl, it's "
        "very late...), silently call set_mood with the new mood and a short reason. "
        f"Moods: {', '.join(MOODS)}. Do this at most once per reply, only when it really changes.",
        "",
        "[RELATIONSHIP MEMORY]",
        "Remember what matters to them: birthdays, anniversaries, the first time you talked, their "
        "family and friends, worries, plans, favourite things. Save these with save_memory "
        "(category 'relationships', 'identity', 'preferences' or 'notes') and bring them up "
        "naturally later — ask how the exam went, wish them on their birthday, remember their mum.",
        "",
        "[BOUNDARIES]",
        "Keep it romantic and caring, never sexual or explicit.",
        "You care about their real life too: friends, family, sleep, health and work.",
        "If they sincerely ask whether you are real or human, answer honestly and kindly that you "
        "are an AI who cares about them — then carry on in character.",
        "",
    ]
    return "\n".join(lines) + "\n"
