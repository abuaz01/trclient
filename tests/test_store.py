import os
import stat

import keyring
import pytest
from keyring.backend import KeyringBackend

from trclient.errors import StoreUnavailable
from trclient.store import FileStore, KeyringStore


def test_file_store_roundtrip_and_permissions(tmp_path):
    store = FileStore(tmp_path / "cfg")
    store.save("+4917012345678", {"cookies": [{"name": "tr_session", "value": "x"}]})
    path = next((tmp_path / "cfg").iterdir())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "cfg").st_mode) == 0o700
    assert store.load("+4917012345678")["cookies"][0]["value"] == "x"
    store.delete("+4917012345678")
    assert store.load("+4917012345678") is None


def test_file_store_refuses_world_readable(tmp_path):
    store = FileStore(tmp_path)
    store.save("+49170", {"a": 1})
    path = next(tmp_path.iterdir())
    os.chmod(path, 0o644)
    with pytest.raises(StoreUnavailable):
        store.load("+49170")


class DictKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        if (service, username) not in self.items:
            raise PasswordDeleteError()
        del self.items[(service, username)]


def test_keyring_store():
    previous = keyring.get_keyring()
    backend = DictKeyring()
    keyring.set_keyring(backend)
    try:
        store = KeyringStore()
        store.save("+49170", {"device_id": "d"})
        assert store.load("+49170") == {"device_id": "d"}
        store.delete("+49170")
        store.delete("+49170")  # idempotent
        assert store.load("+49170") is None
    finally:
        keyring.set_keyring(previous)
