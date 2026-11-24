"""
actions/phone_control.py — control the paired Android phone (Habibi companion app).

The phone app keeps a secure WebSocket open to the dashboard server
(/ws/device). This tool sends one command over it and returns the phone's reply.
Sending an SMS or placing a call goes through core/confirm.py: nothing happens
until the user presses CONFIRM on the HUD.
"""
from __future__ import annotations

import json

from core import confirm

_ACTIONS = [
    "open_app", "list_apps", "open_url", "media", "volume", "flashlight",
    "find_phone", "battery", "notifications", "send_sms", "call", "location",
    "clipboard", "press", "tap_text", "type_text", "read_screen", "status",
]


def _server():
    try:
        from dashboard import server as _srv
        return _srv.ACTIVE_SERVER
    except Exception:
        return None


def _fmt(reply: dict) -> str:
    if not reply.get("ok"):
        return f"Phone error: {reply.get('error') or 'unknown error'}"
    res = reply.get("result")
    if res is None:
        return "Done on the phone."
    if isinstance(res, (dict, list)):
        return json.dumps(res, ensure_ascii=False)[:3500]
    return str(res)[:3500]


def phone_control(parameters: dict, player=None) -> str:
    srv = _server()
    if srv is None:
        return "Phone control is unavailable: the Remote Control server is not running."
    if not srv.device_connected:
        return ("The Habibi phone app is not connected. Open the Habibi app on the phone "
                "and make sure it shows 'Connected'.")

    action = str(parameters.get("action", "")).strip()
    if action not in _ACTIONS:
        return f"Unknown phone action '{action}'. Valid: {', '.join(_ACTIONS)}"

    params = {k: v for k, v in parameters.items() if k != "action" and v not in (None, "")}

    def _run() -> str:
        return _fmt(srv.call_device_sync(action, params))

    if action == "send_sms":
        to, msg = params.get("to", ""), params.get("message", "")
        if not to or not msg:
            return "To send a text I need both 'to' and 'message'."
        return confirm.request(key="phone_sms", title=f"Text {to} from your phone",
                               detail=f"“{msg}”", run=_run)
    if action == "call":
        to = params.get("to", "")
        if not to:
            return "To call I need 'to' (a contact name or number)."
        return confirm.request(key="phone_call", title=f"Call {to} from your phone",
                               detail="Your phone will start the call.", run=_run)

    result = _run()
    if player:
        try:
            player.write_log(f"SYS: Phone › {action} → {result[:160]}")
        except Exception:
            pass
    return result


TOOL = {
    "name": "phone_control",
    "description": (
        "Control the user's Android phone through the Habibi phone app. Use whenever the user "
        "says 'on my phone' or asks about the phone. Actions: "
        "open_app(app) — open an app by name; list_apps; open_url(url); "
        "media(command: play_pause|next|previous|stop); "
        "volume(level 0-100, or change: up|down|mute); flashlight(on: true/false); "
        "find_phone — ring loudly so the user can find it; battery; "
        "notifications — read recent notifications; send_sms(to, message); call(to) — to is a "
        "contact name or number; location; clipboard(text) — copy text to the phone; "
        "press(button: home|back|recents|notifications|lock); tap_text(text) — tap the on-screen "
        "item showing that text; type_text(text) — type into the focused field; "
        "read_screen — read what is on the phone screen; status — phone model, battery, network. "
        "send_sms and call ask the user to confirm on the HUD first; when the result says "
        "confirmation is pending, tell the user to press CONFIRM."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":  {"type": "STRING", "description": "One of: " + ", ".join(_ACTIONS)},
            "app":     {"type": "STRING", "description": "App name for open_app, e.g. WhatsApp"},
            "url":     {"type": "STRING", "description": "Link for open_url"},
            "command": {"type": "STRING", "description": "media: play_pause | next | previous | stop"},
            "level":   {"type": "INTEGER", "description": "volume level 0-100"},
            "change":  {"type": "STRING", "description": "volume: up | down | mute"},
            "on":      {"type": "BOOLEAN", "description": "flashlight on (true) or off (false)"},
            "to":      {"type": "STRING", "description": "contact name or phone number"},
            "message": {"type": "STRING", "description": "SMS text"},
            "text":    {"type": "STRING", "description": "text for clipboard, tap_text or type_text"},
            "button":  {"type": "STRING", "description": "press: home | back | recents | notifications | lock"},
        },
        "required": ["action"],
    },
    "handler": phone_control,
}
