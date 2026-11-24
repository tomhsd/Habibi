from actions.browser_control import send_instagram_dm


TOOL = {
    "name": "send_instagram_dm",
    "description": (
        "Send a direct message on Instagram using the authenticated Playwright browser session. "
        "Use the recipient's username or an Instagram thread ID; the message is sent only after "
        "the DOM message composer accepts it. If username is empty, use the currently open Instagram thread."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "username": {
                "type": "STRING",
                "description": "Instagram username, thread ID, or empty to use the open Instagram thread",
            },
            "message": {
                "type": "STRING",
                "description": "The exact message to send",
            },
            "browser": {
                "type": "STRING",
                "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi",
            },
        },
        "required": ["message"],
    },
    "handler": send_instagram_dm,
}