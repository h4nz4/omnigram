"""Builds synthetic tdata/Pyrogram fixtures with the inverse of each format and imports them."""
import hashlib
import os
import sqlite3
import struct
from contextlib import closing

import pytest
from telethon.crypto import AES
from telethon.sessions import SQLiteSession

from omnigram.importers import (
    _aes_key_iv,
    file_part,
    import_session,
    import_tdata,
    passcode_key,
)


def qbytes(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def tdf(data: bytes) -> bytes:
    version = struct.pack("<i", 4000000)
    return b"TDF$" + version + data + hashlib.md5(data + struct.pack("<i", len(data)) + version + b"TDF$").digest()


def encrypt_local(data: bytes, key: bytes) -> bytes:
    plain = struct.pack("<I", len(data) + 4) + data
    plain += os.urandom(-len(plain) % 16)
    msg_key = hashlib.sha1(plain).digest()[:16]
    return msg_key + AES.encrypt_ige(plain, *_aes_key_iv(key, msg_key))


def read_session(path):
    session = SQLiteSession(str(path))
    try:
        return session.dc_id, session.auth_key.key
    finally:
        session.close()


def test_file_part_matches_tdesktop():
    assert file_part("data") == "D877F783D5D3EF8C"


def test_tdata_with_passcode(tmp_path):
    tdata, out = tmp_path / "tdata", tmp_path / "out"
    tdata.mkdir(), out.mkdir()
    local_key, salt, auth_key = os.urandom(256), os.urandom(32), os.urandom(256)
    info = struct.pack(">iii", 1, 0, 0)  # one account, index 0, active 0
    (tdata / "key_datas").write_bytes(tdf(
        qbytes(salt) + qbytes(encrypt_local(local_key, passcode_key(salt, b"pw"))) + qbytes(encrypt_local(info, local_key))
    ))
    auth = struct.pack(">iiQi", -1, -1, 123456789012, 2) + struct.pack(">ii", 1, 2) + auth_key
    mtp = struct.pack(">i", 0x4B) + qbytes(auth)
    (tdata / "D877F783D5D3EF8Cs").write_bytes(tdf(qbytes(encrypt_local(mtp, local_key))))

    with pytest.raises(ValueError):
        import_tdata(tdata, out)
    [session] = import_tdata(tdata, out, "pw")
    assert session.name == "123456789012.session"
    assert read_session(session) == (2, auth_key)


def test_pyrogram_session_converted(tmp_path):
    src, out = tmp_path / "pyro.session", tmp_path / "out"
    out.mkdir()
    auth_key = os.urandom(256)
    with closing(sqlite3.connect(src)) as db:
        db.execute("CREATE TABLE sessions (dc_id, api_id, test_mode, auth_key, date, user_id, is_bot)")
        db.execute("INSERT INTO sessions VALUES (4, 1, 0, ?, 0, 42, 0)", (auth_key,))
        db.commit()
    assert read_session(import_session(src, out)) == (4, auth_key)
