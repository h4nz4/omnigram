"""Session backup/restore: one .zip holding chosen .session files plus their accounts.json rows."""
import json
import zipfile
from dataclasses import asdict
from pathlib import Path

from omnigram.store import _STORED, Account, Store

METADATA = "accounts.json"


def export_backup(store: Store, accounts: list[Account], dest: Path):
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        meta = {a.session: {k: v for k, v in asdict(a).items() if k in _STORED} for a in accounts}
        zf.writestr(METADATA, json.dumps(meta, ensure_ascii=False, indent=2))
        for account in accounts:
            zf.write(store.path(account), f"{account.session}.session")


def import_backup(store: Store, src: Path) -> tuple[list[str], list[str]]:
    """Returns (imported, skipped) session names; skips any that already exist in the store."""
    imported, skipped = [], []
    with zipfile.ZipFile(src) as zf:
        meta = json.loads(zf.read(METADATA)) if METADATA in zf.namelist() else {}
        for name in zf.namelist():
            if not name.endswith(".session"):
                continue
            stem = name.removesuffix(".session")
            if (store.sessions / name).exists():
                skipped.append(stem)
                continue
            (store.sessions / name).write_bytes(zf.read(name))
            imported.append(stem)
    if imported:
        accounts = {a.session: a for a in store.load()}
        for stem in imported:
            for key, value in meta.get(stem, {}).items():
                if key in _STORED:
                    setattr(accounts[stem], key, value)
        store.save(list(accounts.values()))
    return imported, skipped
