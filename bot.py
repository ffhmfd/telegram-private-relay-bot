#!/usr/bin/env python3
"""Small, dependency-free Telegram private-message relay bot."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from verification import VerificationStore, verification_url


LOG = logging.getLogger("telegram-relay-bot")


class ApiError(RuntimeError):
    def __init__(self, code: int, description: str, retry_after: int = 0):
        super().__init__(description)
        self.code = code
        self.description = description
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        return self.code == 429 or self.code >= 500 or self.code == 0


def redact(value: object, token: str) -> str:
    text = str(value)
    return text.replace(token, "<redacted>") if token else text


class TelegramAPI:
    def __init__(self, token: str, opener: Callable[..., Any] = urllib.request.urlopen):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}/"
        self.opener = opener

    def call(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 20) -> Any:
        request = urllib.request.Request(
            self.base_url + method,
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "telegram-relay-bot/1"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError(exc.code, f"HTTP {exc.code}") from None
            params = data.get("parameters") or {}
            raise ApiError(
                int(data.get("error_code", exc.code)),
                str(data.get("description", f"HTTP {exc.code}")),
                int(params.get("retry_after", 0)),
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(0, exc.__class__.__name__) from None

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(0, "invalid JSON response") from None
        if not data.get("ok"):
            params = data.get("parameters") or {}
            raise ApiError(
                int(data.get("error_code", 0)),
                str(data.get("description", "Telegram API error")),
                int(params.get("retry_after", 0)),
            )
        return data.get("result")


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS message_map (
                owner_message_id INTEGER PRIMARY KEY,
                sender_chat_id INTEGER NOT NULL,
                sender_message_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocked_users (
                sender_chat_id INTEGER PRIMARY KEY,
                blocked_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rate_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_chat_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS rate_events_sender_time
                ON rate_events(sender_chat_id, created_at);
            """
        )
        self.verification = VerificationStore(self.db)

    def close(self) -> None:
        self.db.close()

    def offset(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def set_offset(self, offset: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO meta(key, value) VALUES('offset', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(offset),),
            )

    def add_mapping(self, owner_ids: list[int], sender_id: int, sender_message_id: int, now: int) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO message_map VALUES(?, ?, ?, ?)",
                [(owner_id, sender_id, sender_message_id, now) for owner_id in owner_ids],
            )

    def target_for(self, owner_message_id: int) -> tuple[int, int] | None:
        row = self.db.execute(
            "SELECT sender_chat_id, sender_message_id FROM message_map WHERE owner_message_id=?",
            (owner_message_id,),
        ).fetchone()
        return (int(row[0]), int(row[1])) if row else None

    def is_blocked(self, sender_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM blocked_users WHERE sender_chat_id=?", (sender_id,)
        ).fetchone() is not None

    def block(self, sender_id: int, now: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO blocked_users VALUES(?, ?)", (sender_id, now))
            self.verification.revoke(sender_id)

    def unblock(self, sender_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM blocked_users WHERE sender_chat_id=?", (sender_id,))

    def consume_rate(self, sender_id: int, now: int, count: int, window: int) -> bool:
        cutoff = now - window
        with self.db:
            self.db.execute(
                "DELETE FROM rate_events WHERE sender_chat_id=? AND created_at<=?", (sender_id, cutoff)
            )
            used = int(
                self.db.execute(
                    "SELECT COUNT(*) FROM rate_events WHERE sender_chat_id=?", (sender_id,)
                ).fetchone()[0]
            )
            if used >= count:
                return False
            self.db.execute(
                "INSERT INTO rate_events(sender_chat_id, created_at) VALUES(?, ?)", (sender_id, now)
            )
        return True

    def cleanup(self, now: int, retention_days: int, rate_window: int) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM message_map WHERE created_at<?", (now - retention_days * 86400,)
            )
            self.db.execute("DELETE FROM rate_events WHERE created_at<=?", (now - rate_window,))
        self.verification.cleanup(now)


@dataclass(frozen=True)
class Config:
    owner_id: int
    db_path: str
    retention_days: int = 30
    rate_count: int = 20
    rate_window: int = 60
    verification_url: str = ""


class RelayBot:
    def __init__(self, api: Any, store: Store, config: Config, clock: Callable[[], float] = time.time,
                 bot_id: int = 0):
        self.api = api
        self.store = store
        self.config = config
        self.clock = clock
        self.bot_id = bot_id

    def api_call(self, method: str, payload: dict[str, Any], attempts: int = 4) -> Any:
        for attempt in range(attempts):
            try:
                return self.api.call(method, payload)
            except ApiError as exc:
                if not exc.transient or attempt + 1 == attempts:
                    raise
                delay = exc.retry_after or min(2**attempt, 15)
                time.sleep(delay + random.random())
        raise AssertionError("unreachable")

    def best_effort(self, method: str, payload: dict[str, Any]) -> None:
        try:
            self.api_call(method, payload, attempts=2)
        except ApiError as exc:
            LOG.warning("best-effort %s failed: code=%s", method, exc.code)

    @staticmethod
    def command(message: dict[str, Any]) -> str:
        text = message.get("text") or ""
        if not text.startswith("/"):
            return ""
        return text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()

    @staticmethod
    def identity_text(message: dict[str, Any]) -> str:
        sender = message.get("from") or {}
        name = " ".join(
            part for part in (sender.get("first_name", ""), sender.get("last_name", "")) if part
        ) or "未知"
        username = f"@{sender['username']}" if sender.get("username") else "无"
        kind = next(
            (
                key
                for key in (
                    "text", "photo", "video", "animation", "audio", "voice", "video_note",
                    "document", "sticker", "location", "venue", "contact", "poll", "dice",
                    "live_photo",
                )
                if key in message
            ),
            "其他",
        )
        return (
            "📨 新来信\n"
            f"昵称：{name}\n"
            f"用户名：{username}\n"
            f"用户 ID：{sender.get('id', '未知')}\n"
            f"消息 ID：{message.get('message_id', '未知')}\n"
            f"类型：{kind}"
        )

    def sender_help(self, chat_id: int) -> None:
        self.best_effort(
            "sendMessage",
            {"chat_id": chat_id, "text": "请直接发送留言或媒体。消息成功转交后，我会回复“已送达”。"},
        )

    def owner_help(self) -> None:
        self.best_effort(
            "sendMessage",
            {
                "chat_id": self.config.owner_id,
                "text": "请回复某条来信来匿名答复。回复来信并发送 /block 可封禁，/unblock 可解封。\n恢复旧映射：请选中机器人生成的旧“📨 新来信”身份卡片，回复 /recover；不要选择联系人发送的正文或转发副本。",
            },
        )

    def recover_mapping(self, message: dict[str, Any]) -> None:
        # Explicit owner action only: copied user text can resemble a header,
        # so never infer a destination automatically during ordinary replies.
        reply = message.get("reply_to_message") or {}
        author = reply.get("from") or {}
        text = reply.get("text") or ""
        match = re.fullmatch(
            r"📨 新来信\n昵称：[^\n]*\n用户名：[^\n]*\n"
            r"用户 ID：([1-9][0-9]{0,15})\n消息 ID：([1-9][0-9]{0,15})\n"
            r"类型：(text|photo|video|animation|audio|voice|video_note|document|sticker|"
            r"location|venue|contact|poll|dice|live_photo|其他)", text
        )
        valid = (
            self.bot_id > 0 and author.get("id") == self.bot_id
            and author.get("is_bot") is True and not reply.get("forward_origin")
            and not reply.get("forward_date") and not reply.get("reply_to_message")
            and isinstance(reply.get("message_id"), int) and reply["message_id"] > 0
            and match is not None
        )
        if not valid:
            self.best_effort("sendMessage", {
                "chat_id": self.config.owner_id,
                "text": "无法恢复：请直接回复本机器人原来的“📨 新来信”身份卡片并发送 /recover。请选择含用户 ID、消息 ID 的卡片，不要选正文或转发副本。",
            })
            return
        sender_id, sender_message_id = int(match[1]), int(match[2])
        if sender_id >= 2**52 or sender_id in (self.config.owner_id, self.bot_id):
            self.best_effort("sendMessage", {"chat_id": self.config.owner_id, "text": "无法恢复：卡片用户 ID 无效。"})
            return
        owner_message_id = reply["message_id"]
        existing = self.store.target_for(owner_message_id)
        if existing and existing != (sender_id, sender_message_id):
            self.best_effort("sendMessage", {"chat_id": self.config.owner_id, "text": "恢复已停止：卡片与现有映射冲突，未修改数据库。"})
            return
        self.store.add_mapping([owner_message_id], sender_id, sender_message_id, int(self.clock()))
        LOG.info("owner-requested mapping recovery completed")
        self.best_effort("sendMessage", {
            "chat_id": self.config.owner_id,
            "text": f"✅ 已恢复这张身份卡片 → 用户 {sender_id} 的回复映射，未向对方发消息。请继续回复这张原卡片来联系对方；其下方旧正文的映射尚未恢复。",
        })

    def handle_owner(self, message: dict[str, Any]) -> None:
        command = self.command(message)
        if command == "/recover":
            self.recover_mapping(message)
            return
        if command in ("/start", "/help"):
            self.owner_help()
            return
        reply = message.get("reply_to_message") or {}
        target = self.store.target_for(int(reply.get("message_id", 0))) if reply else None
        if not target:
            self.owner_help()
            return
        sender_id, _ = target
        now = int(self.clock())
        if command == "/block":
            self.store.block(sender_id, now)
            self.best_effort(
                "sendMessage", {"chat_id": self.config.owner_id, "text": f"已封禁用户 {sender_id}。"}
            )
            return
        if command == "/unblock":
            self.store.unblock(sender_id)
            self.best_effort(
                "sendMessage", {"chat_id": self.config.owner_id, "text": f"已解除用户 {sender_id} 的封禁。"}
            )
            return
        try:
            self.api_call(
                "copyMessage",
                {
                    "chat_id": sender_id,
                    "from_chat_id": self.config.owner_id,
                    "message_id": int(message["message_id"]),
                },
            )
        except ApiError as exc:
            if exc.transient:
                raise
            self.best_effort(
                "sendMessage",
                {"chat_id": self.config.owner_id, "text": f"❌ 回复失败（Telegram 错误 {exc.code}）。"},
            )
            return
        self.best_effort(
            "sendMessage", {"chat_id": self.config.owner_id, "text": "✅ 回复已送达。"}
        )

    def handle_sender(self, message: dict[str, Any]) -> None:
        sender = message.get("from") or {}
        sender_id = int(sender.get("id", 0))
        chat_id = int((message.get("chat") or {}).get("id", 0))
        if not sender_id or sender.get("is_bot"):
            return
        if self.store.is_blocked(sender_id):
            self.best_effort("sendMessage", {"chat_id": chat_id, "text": "该消息暂时无法送达。"})
            return
        now = int(self.clock())
        if self.config.verification_url and not self.store.verification.verified(sender_id, now):
            raw = self.store.verification.issue(sender_id, now)
            if raw:
                self.best_effort("sendMessage", {
                    "chat_id": chat_id,
                    "text": "为减少广告，请先完成人机验证。链接 10 分钟内有效；验证通过后请重新发送留言，30 天内无需重复验证。",
                    "reply_markup": {"inline_keyboard": [[{
                        "text": "🛡 点击验证",
                        "url": self.config.verification_url + "/verify#" + raw,
                    }]]},
                })
            return
        if self.command(message) in ("/start", "/help"):
            self.sender_help(chat_id)
            return
        if not self.store.consume_rate(
            sender_id, now, self.config.rate_count, self.config.rate_window
        ):
            self.best_effort(
                "sendMessage", {"chat_id": chat_id, "text": "发送过于频繁，请稍后再试。"}
            )
            return

        header_id = 0
        try:
            header = self.api_call(
                "sendMessage",
                {"chat_id": self.config.owner_id, "text": self.identity_text(message)},
            )
            header_id = int(header["message_id"])
            copied = self.api_call(
                "copyMessage",
                {
                    "chat_id": self.config.owner_id,
                    "from_chat_id": chat_id,
                    "message_id": int(message["message_id"]),
                    "reply_parameters": {"message_id": header_id},
                },
            )
        except ApiError as exc:
            if header_id:
                self.best_effort(
                    "deleteMessage", {"chat_id": self.config.owner_id, "message_id": header_id}
                )
            if exc.transient:
                raise
            self.best_effort(
                "sendMessage",
                {"chat_id": chat_id, "text": "该消息类型暂不支持，或目前无法转交。"},
            )
            return

        copied_id = int(copied["message_id"])
        self.store.add_mapping(
            [header_id, copied_id], sender_id, int(message["message_id"]), now
        )
        self.best_effort("sendMessage", {"chat_id": chat_id, "text": "✅ 已送达。"})

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return
        if int(chat.get("id", 0)) == self.config.owner_id:
            self.handle_owner(message)
        else:
            self.handle_sender(message)


def positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be positive")
    return value


def load_token() -> str:
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    path = Path(credential_dir, "bot-token") if credential_dir else Path(
        os.environ.get("BOT_TOKEN_FILE", "/etc/telegram-relay-bot/token")
    )
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"cannot read bot token file: {path}") from exc
    if not token or ":" not in token:
        raise SystemExit("bot token file is empty or invalid")
    return token


def load_config() -> Config:
    try:
        owner_id = int(os.environ["OWNER_CHAT_ID"])
    except (KeyError, ValueError) as exc:
        raise SystemExit("OWNER_CHAT_ID is missing or invalid") from exc
    state_dir = os.environ.get("STATE_DIRECTORY", "/var/lib/telegram-relay-bot")
    return Config(
        owner_id=owner_id,
        db_path=os.environ.get("BOT_DB_PATH", str(Path(state_dir) / "relay.sqlite3")),
        retention_days=positive_int("MAPPING_RETENTION_DAYS", 30),
        rate_count=positive_int("RATE_LIMIT_COUNT", 20),
        rate_window=positive_int("RATE_LIMIT_WINDOW_SECONDS", 60),
        verification_url=verification_url(os.environ.get("VERIFICATION_URL", "")),
    )


def configure_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")


def initialize(api: TelegramAPI, store: Store) -> None:
    me = api.call("getMe", {})
    api.call("deleteWebhook", {"drop_pending_updates": True})
    store.set_offset(0)
    print(f"Initialized @{me.get('username', 'unknown')} and dropped old pending updates.")


def run(api: TelegramAPI, store: Store, config: Config) -> None:
    me = api.call("getMe", {})
    webhook = api.call("getWebhookInfo", {})
    if webhook.get("url"):
        raise SystemExit("a webhook is configured; run bot.py --init once before starting")
    LOG.info("starting @%s", me.get("username", "unknown"))
    bot = RelayBot(api, store, config, bot_id=int(me["id"]))
    stopped = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_cleanup = 0
    while not stopped:
        now = int(time.time())
        if now - last_cleanup >= 86400:
            store.cleanup(now, config.retention_days, config.rate_window)
            last_cleanup = now
        try:
            updates = api.call(
                "getUpdates",
                {
                    "offset": store.offset(),
                    "limit": 100,
                    "timeout": 25,
                    "allowed_updates": ["message"],
                },
                timeout=35,
            )
            for update in updates:
                try:
                    bot.handle_update(update)
                except ApiError as exc:
                    if exc.transient:
                        raise
                    LOG.warning("discarding update=%s api_code=%s", update.get("update_id"), exc.code)
                except Exception:
                    LOG.exception("discarding malformed update=%s", update.get("update_id"))
                store.set_offset(int(update["update_id"]) + 1)
        except ApiError as exc:
            delay = exc.retry_after or 5
            LOG.warning("telegram temporarily unavailable: code=%s; retrying in %ss", exc.code, delay)
            time.sleep(min(delay, 30))
    LOG.info("stopped")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="clear old webhook and pending test updates")
    args = parser.parse_args()
    configure_logging()
    token = load_token()
    config = load_config()
    api = TelegramAPI(token)
    store = Store(config.db_path)
    try:
        if args.init:
            initialize(api, store)
        else:
            run(api, store, config)
    except ApiError as exc:
        LOG.error("Telegram API failure: code=%s description=%s", exc.code, redact(exc.description, token))
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
