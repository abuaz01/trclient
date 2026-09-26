"""Where the session (cookies + device id) is kept between runs.

The PIN is never stored anywhere. The session cookies are bearer credentials for the
account, so by default they go into the OS keychain (macOS Keychain, Windows Credential
Locker, Secret Service on Linux). A 0600 file is available for headless machines
without a keyring, and must be chosen explicitly (TR_SESSION_STORE=file).
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
from abc import ABC, abstractmethod

from .errors import StoreUnavailable

KEYRING_SERVICE = "trclient"


class SessionStore(ABC):
    @abstractmethod
    def load(self, account: str) -> dict | None: ...

    @abstractmethod
    def save(self, account: str, data: dict) -> None: ...

    @abstractmethod
    def delete(self, account: str) -> None: ...


class MemoryStore(SessionStore):
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def load(self, account: str) -> dict | None:
        raw = self.data.get(account)
        return json.loads(raw) if raw else None

    def save(self, account: str, data: dict) -> None:
        self.data[account] = json.dumps(data)

    def delete(self, account: str) -> None:
        self.data.pop(account, None)


class KeyringStore(SessionStore):
    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        import keyring
        from keyring.backends import fail

        if isinstance(keyring.get_keyring(), fail.Keyring):
            raise StoreUnavailable(
                "No OS keyring available. Install one, or set TR_SESSION_STORE=file "
                "to keep the session in a 0600 file instead."
            )
        self._keyring = keyring
        self.service = service

    def load(self, account: str) -> dict | None:
        raw = self._keyring.get_password(self.service, account)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def save(self, account: str, data: dict) -> None:
        self._keyring.set_password(self.service, account, json.dumps(data))

    def delete(self, account: str) -> None:
        from keyring.errors import PasswordDeleteError

        try:
            self._keyring.delete_password(self.service, account)
        except PasswordDeleteError:
            pass


class FileStore(SessionStore):
    """One JSON file per account, owner-only permissions, written atomically."""

    def __init__(self, directory: str | os.PathLike) -> None:
        self.directory = pathlib.Path(directory).expanduser()

    def _path(self, account: str) -> pathlib.Path:
        safe = "".join(ch for ch in account if ch.isalnum())
        return self.directory / f"session-{safe}.json"

    def load(self, account: str) -> dict | None:
        path = self._path(account)
        if not path.exists():
            return None
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise StoreUnavailable(f"{path} is readable by other users - fix with: chmod 600 {path}")
        try:
            return json.loads(path.read_text())
        except ValueError:
            return None

    def save(self, account: str, data: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        path = self._path(account)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)

    def delete(self, account: str) -> None:
        self._path(account).unlink(missing_ok=True)


def default_store() -> SessionStore:
    kind = os.environ.get("TR_SESSION_STORE", "keyring").strip().lower()
    if kind == "file":
        return FileStore(os.environ.get("TR_SESSION_DIR", "~/.config/trclient"))
    if kind == "keyring":
        return KeyringStore()
    raise StoreUnavailable(f"Unknown TR_SESSION_STORE {kind!r} (use 'keyring' or 'file')")
