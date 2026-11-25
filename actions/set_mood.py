"""
actions/set_mood.py — Habibi changes her own mood.

Called silently by the model when something in the conversation changes how
it feels. The mood is saved (config/mood.json), shown on the HUD reactor and
sent to the phone app.
"""
from __future__ import annotations

from core import persona


def announce_mood(player=None) -> None:
    """Push the current mood to the PC HUD and to connected phones."""
    key, label, color = persona.mood_view()
    if player is not None:
        try:
            player.set_mood(key, label, color)
        except Exception:
            pass
    try:
        import asyncio
        from dashboard import server as _srv
        srv = _srv.ACTIVE_SERVER
        if srv is not None and srv._loop is not None:
            asyncio.run_coroutine_threadsafe(
                srv.broadcast({"type": "mood", "mood": key, "label": label, "color": color}),
                srv._loop,
            )
    except Exception:
        pass


def set_mood(parameters: dict, player=None) -> str:
    try:
        m = persona.set_mood(str(parameters.get("mood", "")), str(parameters.get("reason", "")))
    except ValueError as e:
        return str(e)
    announce_mood(player)
    return f"Mood is now {m['mood']}. Keep talking naturally — do not mention this tool."


TOOL = {
    "name": "set_mood",
    "description": (
        "Change YOUR OWN current mood/feeling when something in the conversation genuinely "
        "changes how you feel. Call it silently (never say you are calling it), at most once "
        "per reply. Moods: " + ", ".join(persona.MOODS) + "."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mood":   {"type": "STRING", "description": "One of: " + ", ".join(persona.MOODS)},
            "reason": {"type": "STRING", "description": "Short reason, e.g. 'he got the job'"},
        },
        "required": ["mood"],
    },
    "handler": set_mood,
}
