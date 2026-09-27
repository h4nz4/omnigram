import json

from omnigram.importers import read_session_json
from omnigram.store import Store


def test_read_session_json(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"app_id": 2040, "app_hash": "abc", "phone": "+84993297918", "first_name": "A",
                                "last_name": None, "username": "", "twoFA": "secret", "device": "PC"}), "utf-8")
    fields = read_session_json(path)
    assert fields == {"api_id": 2040, "api_hash": "abc", "phone": "84993297918", "name": "A"}  # no 2FA, no extras
    path.write_text(json.dumps({"api_id": "7", "api_hash": "h"}), "utf-8")
    assert read_session_json(path) == {"api_id": 7, "api_hash": "h"}


def test_account_api_credentials_persist(tmp_path):
    store = Store(tmp_path)
    (store.sessions / "a.session").touch()
    [a] = store.load()
    assert (a.api_id, a.api_hash) == (0, "")
    a.api_id, a.api_hash = 2040, "abc"
    store.save([a])
    assert store.load()[0].api_id == 2040
