"""
Checks the bot's new Telegram messages for an on-demand request ("now").

Runs every few minutes from GitHub Actions. Uses only the standard library so
the check is fast and needs no installs. Only chats listed in TELEGRAM_CHAT_ID
can trigger a report. If a request is found, writes `chat=<id>` to
$GITHUB_OUTPUT so the next workflow step runs the digest for that chat.
"""

import json
import os
import urllib.parse
import urllib.request

TRIGGERS = {"now", "/now", "الان", "الآن", "تقرير", "/report"}


def api(token, method, **params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=30) as r:
        return json.load(r)


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    allowed = {c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()}

    updates = api(token, "getUpdates", timeout=0).get("result", [])
    if not updates:
        print("no new messages")
        return

    requested_chat = None
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        text = (msg.get("text") or "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if text.split("@")[0] in TRIGGERS:
            if chat_id in allowed:
                requested_chat = chat_id
            else:
                print(f"ignored request from unknown chat {chat_id}")

    # mark these messages as handled so they are not picked up again
    api(token, "getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)

    if not requested_chat:
        print(f"{len(updates)} message(s), no report request")
        return

    api(token, "sendMessage", chat_id=requested_chat,
        text="⏳ جاري تجهيز التقرير… يوصلك خلال دقيقتين.")
    print(f"report requested by chat {requested_chat}")
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
        f.write(f"chat={requested_chat}\n")


if __name__ == "__main__":
    main()
