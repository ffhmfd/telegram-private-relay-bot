#!/usr/bin/env python3
"""Find private-chat user IDs without storing or echoing the Bot Token."""

import getpass
import json
import sys
import urllib.error
import urllib.request


def call(token, method, payload=None, timeout=40):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("Could not connect to Telegram") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error {result.get('error_code', 'unknown')}")
    return result.get("result")


def main():
    token = getpass.getpass("Telegram Bot Token: ").strip()
    if not token or ":" not in token:
        print("Invalid Bot Token format.", file=sys.stderr)
        return 1
    try:
        bot = call(token, "getMe")
        webhook = call(token, "getWebhookInfo")
        if webhook.get("url"):
            print("This bot already has a webhook. Refusing to interfere with it.", file=sys.stderr)
            return 1
        print(f"Connected to @{bot.get('username', 'unknown')}.")
        print("Send a private message to this bot now; waiting up to 30 seconds...")
        updates = call(
            token,
            "getUpdates",
            {"timeout": 30, "limit": 100, "allowed_updates": ["message"]},
            timeout=40,
        )
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        token = ""

    users = {}
    for update in updates:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") == "private" and sender.get("id") and not sender.get("is_bot"):
            users[sender["id"]] = sender
    if not users:
        print("No private user message found. Send a message and run the command again.")
        return 1
    print("Private users found:")
    for user_id, user in users.items():
        name = " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))
        username = "@" + user["username"] if user.get("username") else "(no username)"
        print(f"  ID: {user_id}  Name: {name or '(unknown)'}  Username: {username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
