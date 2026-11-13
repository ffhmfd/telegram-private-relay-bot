# Security Policy

## Secrets

Never commit a Bot Token, private key, real `OWNER_CHAT_ID`, production configuration, SQLite database, server address, or log archive. The supplied `.gitignore` blocks common secret and runtime file names, but always review `git diff --cached` before pushing.

Never commit a production Turnstile Secret Key, key export, or private deployment notes. A Site Key is public by design, but examples should still use placeholders rather than a live deployment's values. Inspect commit history as well as the current tree before publishing a development branch.

The installer reads the Bot Token without terminal echo and stores it in `/etc/telegram-relay-bot/token` with mode `0600`. systemd delivers it as a runtime credential. It is not placed in the source tree, service command line, or regular service environment.

When verification is enabled, store the Turnstile secret in `/etc/telegram-relay-bot/turnstile-secret`, owned by root with mode `0600`. The verifier receives it through a separate systemd credential. Rotate a leaked secret through Cloudflare, replace this file, and restart `telegram-relay-verify.service`.

If a Bot Token may have leaked:

1. Revoke it immediately through [@BotFather](https://t.me/BotFather).
2. Generate a replacement Token.
3. Replace `/etc/telegram-relay-bot/token` and keep its mode at `0600`.
4. Restart `telegram-relay-bot.service`.
5. Remove the secret from Git history if it was committed; deleting only the latest file is insufficient.

## Reporting a vulnerability

Do not put secrets or exploitable production details in a public issue. Open a minimal issue asking the maintainer for a private contact channel, or use GitHub private vulnerability reporting if it is enabled for the repository.

## Operational scope

This bot intentionally uses one administrator, private chats, Telegram-hosted media, outbound HTTPS long polling, per-user rate limiting, and a local SQLite mapping database. Operators remain responsible for system updates, SSH security, backups, legal compliance, and reviewing access to the VPS.

Optional Turnstile verification adds an HTTPS page backed by a loopback-only service. It validates hostname, action and challenge binding on the server, fails closed on validation errors, and stores only challenge digests and grant expiry times. Challenge links are bearer capabilities: sharing a link can only verify its original sender, not the person opening it. Human-operated spam is still possible; administrator blocking remains available.
