"""Telegram startup notification."""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request


def send_startup_notification(bot_token: str, chat_id: int, context_hint: str) -> None:
    message = f"🤖 Claude Code session started\nContext: {context_hint}"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": str(chat_id), "text": message}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                print(f"Telegram error: {result}", file=sys.stderr)
            else:
                print("Notification sent.")
    except Exception as e:
        print(f"Failed to send notification: {e}", file=sys.stderr)
