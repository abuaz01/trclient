"""Non-secret local configuration (only the phone number of the last login)."""

from __future__ import annotations

import json
import os
import pathlib

CONFIG_FILE = pathlib.Path(os.environ.get("TR_CONFIG_FILE", "~/.config/trclient/config.json")).expanduser()


def stored_phone() -> str | None:
    phone = os.environ.get("TR_PHONE", "").strip()
    if phone:
        return phone
    try:
        return json.loads(CONFIG_FILE.read_text()).get("phone") or None
    except (OSError, ValueError):
        return None


def save_phone(phone: str) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"phone": phone}, fh)
