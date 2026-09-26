"""The X-TR-Device-Info header the v2 login endpoints require.

Without it they answer 400 MISSING_REQUIRED_HEADER. The id inside has to stay the same
across logins, otherwise every login looks like a new device - so it is generated once
and persisted in the session store.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import secrets
from datetime import datetime


def new_device_id() -> str:
    # Same length and alphabet as the frontend's SHA-512 hex fingerprint.
    return secrets.token_hex(64)


def _timezone_name() -> str:
    tz = os.environ.get("TZ", "").strip()
    if "/" in tz:
        return tz.lstrip(":")
    try:
        return str(pathlib.Path("/etc/localtime").resolve()).split("zoneinfo/")[1]
    except (OSError, IndexError):
        return "Europe/Berlin"


def _os_from_user_agent(user_agent: str) -> tuple[str, str]:
    # Keep the device description consistent with the User-Agent that is sent.
    if "Macintosh" in user_agent:
        return "Mac OS", "10.15.7"
    if "Windows" in user_agent:
        return "Windows", "10"
    return "Linux", "x86_64"


def build_device_info(device_id: str, user_agent: str, locale: str) -> str:
    chrome = re.search(r"Chrome/([\d.]+)", user_agent)
    offset = datetime.now().astimezone().utcoffset()
    os_name, os_version = _os_from_user_agent(user_agent)
    device = {
        "stableDeviceId": device_id,
        "browser": "Chrome",
        "browserVersion": chrome.group(1) if chrome else "",
        "os": os_name,
        "osVersion": os_version,
        "timezone": _timezone_name(),
        # JavaScript's getTimezoneOffset() has the opposite sign of Python's utcoffset().
        "timezoneOffset": -int(offset.total_seconds() // 60) if offset else 0,
        "screen": "1920x1080x24",
        "preferredLanguages": [locale],
        "numberOfCores": os.cpu_count() or 1,
    }
    return base64.b64encode(json.dumps(device).encode()).decode("ascii")
