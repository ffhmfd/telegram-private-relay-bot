#!/usr/bin/env python3
"""One-use Turnstile challenges; shared by the bot and loopback web service."""

from __future__ import annotations

import hashlib
import argparse
import html
import json
import logging
import os
import re
import secrets
import signal
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


LOG = logging.getLogger('telegram-relay-verify')


def verification_url(value: str) -> str:
    """Accept an HTTPS origin only; empty explicitly disables the optional gate."""
    if not value:
        return ''
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ('', '/') or parsed.query
            or parsed.fragment or parsed.port not in (None, 443)):
        raise ValueError('VERIFICATION_URL must be an HTTPS origin without path or credentials')
    return 'https://' + parsed.netloc.rstrip('/')


class VerificationStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS blocked_users (
                sender_chat_id INTEGER PRIMARY KEY, blocked_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verification_challenges (
                user_id INTEGER PRIMARY KEY,
                digest TEXT NOT NULL UNIQUE,
                expires_at INTEGER NOT NULL,
                prompt_after INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                attempt_after INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS verified_users (
                user_id INTEGER PRIMARY KEY, expires_at INTEGER NOT NULL
            );
        ''')

    @staticmethod
    def digest(raw: object) -> str | None:
        if not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw):
            return None
        return hashlib.sha256(raw.encode('ascii')).hexdigest()

    def verified(self, user_id: int, now: int) -> bool:
        return self.db.execute(
            'SELECT 1 FROM verified_users WHERE user_id=? AND expires_at>? '
            'AND NOT EXISTS (SELECT 1 FROM blocked_users WHERE sender_chat_id=?)',
            (user_id, now, user_id),
        ).fetchone() is not None

    def issue(self, user_id: int, now: int) -> str | None:
        raw = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute('SELECT 1 FROM blocked_users WHERE sender_chat_id=?', (user_id,)).fetchone():
                return None
            row = self.db.execute(
                'SELECT prompt_after FROM verification_challenges WHERE user_id=?', (user_id,)
            ).fetchone()
            if row and row[0] > now:
                return None
            self.db.execute(
                'INSERT OR REPLACE INTO verification_challenges VALUES (?, ?, ?, ?, 0, 0)',
                (user_id, self.digest(raw), now + 600, now + 60),
            )
        return raw

    def challenge(self, raw: object, now: int) -> str | None:
        digest = self.digest(raw)
        if digest is None:
            return None
        row = self.db.execute(
            'SELECT digest FROM verification_challenges c WHERE digest=? AND expires_at>? '
            'AND NOT EXISTS (SELECT 1 FROM blocked_users WHERE sender_chat_id=c.user_id)',
            (digest, now),
        ).fetchone()
        return row[0] if row else None

    def reserve(self, raw: object, now: int) -> str | None:
        digest = self.digest(raw)
        if digest is None:
            return None
        with self.db:
            result = self.db.execute(
                'UPDATE verification_challenges SET attempts=attempts+1, attempt_after=? '
                'WHERE digest=? AND expires_at>? AND attempts<5 AND attempt_after<=? '
                'AND NOT EXISTS (SELECT 1 FROM blocked_users WHERE sender_chat_id=user_id)',
                (now + 3, digest, now, now),
            )
        return digest if result.rowcount == 1 else None

    def complete(self, raw: object, now: int) -> bool:
        digest = self.digest(raw)
        if digest is None:
            return False
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute(
                'SELECT user_id FROM verification_challenges c '
                'WHERE digest=? AND expires_at>? AND attempts>0 '
                'AND NOT EXISTS (SELECT 1 FROM blocked_users WHERE sender_chat_id=c.user_id)',
                (digest, now),
            ).fetchone()
            if not row:
                return False
            self.db.execute('INSERT OR REPLACE INTO verified_users VALUES (?, ?)', (row[0], now + 30 * 86400))
            self.db.execute('DELETE FROM verification_challenges WHERE digest=?', (digest,))
        return True

    def revoke(self, user_id: int) -> None:
        # Caller may already own a transaction (block + revoke must be atomic).
        self.db.execute('DELETE FROM verified_users WHERE user_id=?', (user_id,))
        self.db.execute('DELETE FROM verification_challenges WHERE user_id=?', (user_id,))

    def cleanup(self, now: int) -> None:
        with self.db:
            self.db.execute('DELETE FROM verification_challenges WHERE expires_at<=?', (now,))
            self.db.execute('DELETE FROM verified_users WHERE expires_at<=?', (now,))


class TurnstileClient:
    def __init__(self, secret: str, hostname: str, opener=urllib.request.urlopen):
        self.secret = secret
        self.hostname = hostname
        self.opener = opener

    def verify(self, token: object, digest: str) -> bool:
        if not isinstance(token, str) or not 1 <= len(token) <= 2048:
            return False
        request = urllib.request.Request(
            'https://challenges.cloudflare.com/turnstile/v0/siteverify',
            data=json.dumps({'secret': self.secret, 'response': token}).encode(),
            headers={'Content-Type': 'application/json', 'User-Agent': 'telegram-relay-verify/1'},
            method='POST',
        )
        try:
            with self.opener(request, timeout=10) as response:
                result = json.loads(response.read(16385))
            return (isinstance(result, dict) and result.get('success') is True
                    and result.get('hostname') == self.hostname
                    and result.get('action') == 'private_relay'
                    and result.get('cdata') == digest)
        except (OSError, urllib.error.URLError, ValueError):
            LOG.warning('Cloudflare validation unavailable or invalid response')
            return False


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    request_deadline = 10

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        LOG.warning('verification HTTP request failed')


def make_server(address, db_path: str, site_key: str, client: TurnstileClient, clock=time.time):
    if address[0] != '127.0.0.1':
        raise ValueError('verification server must listen on 127.0.0.1')
    web = Path(__file__).with_name('web')
    page = (web / 'verify.html').read_text(encoding='utf-8').replace('{{SITE_KEY}}', html.escape(site_key, quote=True)).encode()
    assets = {
        '/': (page, 'text/html; charset=utf-8'),
        '/verify': (page, 'text/html; charset=utf-8'),
        '/verify.css': ((web / 'verify.css').read_bytes(), 'text/css; charset=utf-8'),
        '/verify.js': ((web / 'verify.js').read_bytes(), 'application/javascript; charset=utf-8'),
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = 'RelayVerify'
        sys_version = ''

        def setup(self):
            self.request.settimeout(10)
            super().setup()
            # A socket timeout is only an inactivity timeout. Close a trickled
            # request on an absolute deadline as well, including slow headers.
            self.deadline = threading.Timer(self.server.request_deadline, self.expire_request)
            self.deadline.daemon = True
            self.deadline.start()

        def expire_request(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def finish(self):
            self.deadline.cancel()
            super().finish()

        def log_message(self, _format, *args):
            # Never log URL capabilities, request bodies, or upstream tokens.
            pass

        def respond(self, status, data, content_type='application/json; charset=utf-8'):
            body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, dict) else data
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self' https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; connect-src 'self' https://challenges.cloudflare.com; style-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def error(self, status, message):
            self.respond(status, {'ok': False, 'message': message})

        def do_GET(self):
            self.deadline.cancel()
            asset = assets.get(self.path)
            if asset is None:
                self.error(404, '页面不存在。')
            else:
                self.respond(200, *asset)

        def do_POST(self):
            db = None
            try:
                if self.path not in ('/api/challenge', '/api/verify'):
                    self.error(404, '页面不存在。')
                    return
                if self.headers.get_all('Origin') != ['https://' + client.hostname]:
                    self.error(403, '请从机器人提供的验证链接打开。')
                    return
                if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                    self.error(415, '请求格式不正确。')
                    return
                lengths = self.headers.get_all('Content-Length') or []
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or self.headers.get('Transfer-Encoding'):
                    self.error(400, '请求长度不正确。')
                    return
                if len(lengths[0]) > 6 or int(lengths[0]) > 8192:
                    self.error(413, '请求过大。')
                    return
                length = int(lengths[0])
                body = self.rfile.read(length)
                self.deadline.cancel()
                if len(body) != length:
                    self.error(400, '请求不完整。')
                    return
                try:
                    payload = json.loads(body, object_pairs_hook=strict_object)
                except (ValueError, UnicodeError, RecursionError):
                    self.error(400, '请求格式不正确。')
                    return
                if not isinstance(payload, dict):
                    self.error(400, '请求格式不正确。')
                    return
                raw = payload.get('challenge')
                db = sqlite3.connect(db_path, timeout=5)
                state = VerificationStore(db)
                now = int(clock())
                if self.path == '/api/challenge':
                    digest = state.challenge(raw, now)
                    if digest is None:
                        self.error(410, '链接已失效，请回到机器人发送 /start 获取新链接。')
                    else:
                        self.respond(200, {'ok': True, 'cdata': digest})
                    return
                token = payload.get('token')
                if not isinstance(token, str) or not 1 <= len(token) <= 2048:
                    self.error(400, '验证响应无效，请重新验证。')
                    return
                digest = state.reserve(raw, now)
                if digest is None:
                    self.error(429, '链接失效或尝试过于频繁。请稍后重试，或回到机器人发送 /start。')
                    return
                if not client.verify(token, digest):
                    self.error(403, '验证未通过或服务暂不可用，请稍后重新验证。')
                    return
                if not state.complete(raw, int(clock())):
                    self.error(410, '链接已失效，请回到机器人发送 /start 获取新链接。')
                    return
                self.respond(200, {'ok': True, 'message': '验证通过！请回到 Telegram，重新发送刚才的留言。30 天内无需重复验证。'})
            except (TimeoutError, ConnectionError):
                self.close_connection = True
            except sqlite3.Error:
                LOG.warning('verification storage temporarily unavailable')
                self.error(503, '服务暂不可用，请稍后重试。')
            finally:
                if db is not None:
                    db.close()

    return BoundedHTTPServer(address, Handler)


def load_web_config():
    origin = verification_url(os.environ.get('VERIFICATION_URL', ''))
    site_key = os.environ.get('TURNSTILE_SITE_KEY', '').strip()
    if not origin or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}', site_key):
        raise SystemExit('VERIFICATION_URL and TURNSTILE_SITE_KEY are required')
    if re.match(r'^[123]x0{10}', site_key):
        raise SystemExit('production service refuses Cloudflare testing site keys')
    credential_dir = os.environ.get('CREDENTIALS_DIRECTORY')
    secret_path = Path(credential_dir, 'turnstile-secret') if credential_dir else Path(
        os.environ.get('TURNSTILE_SECRET_FILE', '/etc/telegram-relay-bot/turnstile-secret'))
    try:
        secret = secret_path.read_text(encoding='utf-8').strip()
    except OSError:
        raise SystemExit('cannot read Turnstile secret credential') from None
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,200}', secret) or re.match(r'^[123]x0{10}', secret):
        raise SystemExit('invalid production Turnstile secret')
    host = os.environ.get('VERIFY_HOST', '127.0.0.1')
    port = int(os.environ.get('VERIFY_PORT', '8765'))
    if host != '127.0.0.1' or not 1 <= port <= 65535:
        raise SystemExit('verification listener must use loopback and a valid port')
    state_dir = os.environ.get('STATE_DIRECTORY', '/var/lib/telegram-relay-bot')
    return {
        'address': (host, port),
        'db_path': os.environ.get('BOT_DB_PATH', str(Path(state_dir) / 'relay.sqlite3')),
        'site_key': site_key,
        'client': TurnstileClient(secret, urlsplit(origin).hostname),
    }


def check_configuration() -> bool:
    if not os.environ.get('VERIFICATION_URL', ''):
        return False
    load_web_config()
    return True


def check_readiness(config, attempts=20):
    host, port = config['address']
    marker = ('data-sitekey="' + html.escape(config['site_key'], quote=True) + '"').encode()
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(f'http://{host}:{port}/verify', timeout=2) as response:
                if marker in response.read(16384):
                    return
        except OSError:
            pass
        if attempt + 1 < attempts:
            time.sleep(0.25)
    raise SystemExit('verification HTTP readiness failed; inspect telegram-relay-verify.service')


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check-config', action='store_true')
    group.add_argument('--check-ready', action='store_true')
    args = parser.parse_args()
    if args.check_config:
        print('enabled' if check_configuration() else 'disabled')
        return
    config = load_web_config()
    if args.check_ready:
        check_readiness(config)
        return
    db = sqlite3.connect(config['db_path'], timeout=10)
    try:
        VerificationStore(db).cleanup(int(time.time()))
    finally:
        db.close()
    server = make_server(**config)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    LOG.info('verification service listening on loopback')
    try:
        stopped.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    main()
