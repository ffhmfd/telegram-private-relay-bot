#!/usr/bin/env python3
"""Distinguish fresh installs from repairs without modifying existing state."""

from pathlib import Path
import sys


def first_install(config: Path, app: Path, state: Path) -> bool:
    token, settings = config / 'token', config / 'config'
    if token.is_file() and settings.is_file() and token.read_bytes().strip() and settings.read_bytes().strip():
        return False
    if any(path.exists() for path in (config, app, state)):
        raise ValueError(
            'Existing installation is incomplete. Restore nonempty token and config '
            'files in /etc/telegram-relay-bot, then retry. Existing configuration '
            'and pending Telegram updates have not been changed.'
        )
    return True


if __name__ == '__main__':
    try:
        print(int(first_install(*(Path(value) for value in sys.argv[1:4]))))
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
