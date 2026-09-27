"""Convert Pyrogram/Telethon session files and Telegram Desktop tdata folders into Telethon .session files."""
import hashlib
import json
import shutil
import sqlite3
import struct
from contextlib import closing
from pathlib import Path

from telethon.crypto import AES, AuthKey
from telethon.sessions import SQLiteSession

# Production DC addresses; Telethon replaces them with fresh ones after connecting.
DC_IPS = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100", 4: "149.154.167.91", 5: "91.108.56.130"}
MTP_AUTHORIZATION = 0x4B  # tdesktop dbiMtpAuthorization block id


def write_session(path: Path, dc_id: int, auth_key: bytes) -> Path:
    session = SQLiteSession(str(path))
    session.set_dc(dc_id, DC_IPS[dc_id], 443)
    session.auth_key = AuthKey(auth_key)
    session.save()
    session.close()
    return path


def import_session(src: Path, dest_dir: Path) -> Path:
    """Copy a Telethon session, or convert a Pyrogram one (same table name, different columns)."""
    with closing(sqlite3.connect(src)) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        if not columns:
            raise ValueError(f"{src.name}: not a Telethon or Pyrogram session")
        if "server_address" in columns:
            return Path(shutil.copy(src, dest_dir / src.name))
        dc_id, auth_key = db.execute("SELECT dc_id, auth_key FROM sessions").fetchone()
    return write_session(dest_dir / src.name, dc_id, auth_key)


def read_session_json(path: Path) -> dict:
    """Account fields from the JSON that often ships next to a .session (app_id/app_hash, phone, name).

    Only these are read; anything else in the file (including any stored 2FA password) is ignored.
    """
    data = json.loads(path.read_text("utf-8"))
    fields = {
        "api_id": int(data.get("app_id") or data.get("api_id") or 0),
        "api_hash": str(data.get("app_hash") or data.get("api_hash") or ""),
        "phone": str(data.get("phone") or "").lstrip("+"),
        "name": " ".join(filter(None, [data.get("first_name"), data.get("last_name")])),
        "username": str(data.get("username") or ""),
    }
    return {key: value for key, value in fields.items() if value}


class _Stream:
    """Reader for Qt QDataStream payloads (big-endian)."""

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def read(self, fmt: str):
        (value,) = struct.unpack_from(">" + fmt, self.data, self.pos)
        self.pos += struct.calcsize(">" + fmt)
        return value

    def raw(self, n: int) -> bytes:
        self.pos += n
        return self.data[self.pos - n : self.pos]

    def bytes(self) -> bytes:  # QByteArray: uint32 length, 0xFFFFFFFF means null
        n = self.read("I")
        return b"" if n == 0xFFFFFFFF else self.raw(n)


def file_part(name: str) -> str:
    """tdesktop ToFilePart: md5 nibbles, low first. file_part("data") == "D877F783D5D3EF8C"."""
    return "".join(f"{b & 0xF:X}{b >> 4:X}" for b in hashlib.md5(name.encode()).digest()[:8])


def read_tdf(base: Path) -> bytes:
    """Read a tdesktop TDF$ file: magic, int32 version, data, md5 over data+len+version+magic."""
    # ponytail: first valid suffix wins; tdesktop picks the highest version if several exist
    for suffix in "s10":
        path = base.with_name(base.name + suffix)
        raw = path.read_bytes() if path.exists() else b""
        if raw[:4] != b"TDF$":
            continue
        version, data, digest = raw[4:8], raw[8:-16], raw[-16:]
        if hashlib.md5(data + struct.pack("<i", len(data)) + version + b"TDF$").digest() == digest:
            return data
    raise FileNotFoundError(f"no valid tdata file for {base.name}")


def _aes_key_iv(key: bytes, msg_key: bytes) -> tuple[bytes, bytes]:
    """tdesktop prepareAES_oldmtp, receive direction."""
    sha1 = lambda b: hashlib.sha1(b).digest()
    a = sha1(msg_key + key[8:40])
    b = sha1(key[40:56] + msg_key + key[56:72])
    c = sha1(key[72:104] + msg_key)
    d = sha1(msg_key + key[104:136])
    return a[:8] + b[8:20] + c[4:16], a[8:20] + b[:8] + c[16:20] + d[:8]


def decrypt_local(data: bytes, key: bytes) -> bytes:
    msg_key = data[:16]
    plain = AES.decrypt_ige(data[16:], *_aes_key_iv(key, msg_key))
    if hashlib.sha1(plain).digest()[:16] != msg_key:
        raise ValueError("wrong passcode or corrupted tdata")
    (size,) = struct.unpack_from("<I", plain)  # size includes these 4 bytes
    return plain[4:size]


def passcode_key(salt: bytes, passcode: bytes) -> bytes:
    digest = hashlib.sha512(salt + passcode + salt).digest()
    return hashlib.pbkdf2_hmac("sha512", digest, salt, 100_000 if passcode else 1, 256)


def import_tdata(tdata: Path, dest_dir: Path, passcode: str = "") -> list[Path]:
    """Write one <user_id>.session per account in the tdata folder. ValueError = passcode needed/wrong."""
    keys = _Stream(read_tdf(tdata / "key_data"))
    salt, key_encrypted, info_encrypted = keys.bytes(), keys.bytes(), keys.bytes()
    local_key = decrypt_local(key_encrypted, passcode_key(salt, passcode.encode()))
    info = _Stream(decrypt_local(info_encrypted, local_key))
    indexes = [info.read("i") for _ in range(info.read("i"))]

    sessions = []
    for index in indexes:
        name = file_part("data" if index == 0 else f"data#{index + 1}")
        mtp = _Stream(decrypt_local(_Stream(read_tdf(tdata / name)).bytes(), local_key))
        if mtp.read("i") != MTP_AUTHORIZATION:
            raise ValueError(f"account {index}: no MTP authorization block")
        auth = _Stream(mtp.bytes())
        user_id, dc_id = auth.read("i"), auth.read("i")
        if user_id == dc_id == -1:  # kWideIdsTag: 64-bit user id follows
            user_id, dc_id = auth.read("Q"), auth.read("i")
        auth_keys = {auth.read("i"): auth.raw(256) for _ in range(auth.read("i"))}
        sessions.append(write_session(dest_dir / f"{user_id}.session", dc_id, auth_keys[dc_id]))
    return sessions
