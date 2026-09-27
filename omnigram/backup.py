"""Session backup/restore: one .zip holding chosen .session files plus their accounts.json rows, and optionally
more of each account's files (AI profile, saved jobs) under data/ — a server's backup carries those; restoring
writes them only for the sessions it imports."""
import json
import zipfile
from dataclasses import asdict
from pathlib import Path

from omnigram.store import _STORED, Account, Store

METADATA = "accounts.json"


def export_backup(store: Store, accounts: list[Account], dest: Path, extra: dict[str, bytes] | None = None):
    """`extra`: data-relative path -> content (e.g. "ai/x.json"), stored under data/."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        meta = {a.session: {k: v for k, v in asdict(a).items() if k in _STORED} for a in accounts}
        zf.writestr(METADATA, json.dumps(meta, ensure_ascii=False, indent=2))
        for account in accounts:
            zf.write(store.path(account), f"{account.session}.session")
        for rel, data in (extra or {}).items():
            zf.writestr(f"data/{rel}", data)


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
        root = store.sessions.parent
        for name in zf.namelist():
            rel = name.removeprefix("data/")
            if name.startswith("data/") and _restorable(rel, imported) and not (root / rel).exists():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_bytes(zf.read(name))
    if imported:
        accounts = {a.session: a for a in store.load()}
        for stem in imported:
            for key, value in meta.get(stem, {}).items():
                if key in _STORED:
                    setattr(accounts[stem], key, value)
            accounts[stem].placement = "local"  # restored here: it connects from here
        store.save(list(accounts.values()))
    return imported, skipped


def _restorable(rel: str, imported: list[str]) -> bool:
    """An extra file belonging to an imported session (its AI profile or a saved job), or a funnel."""
    from omnigram.engine import account_file_path
    for session in imported:
        try:
            account_file_path(session, rel)
        except ValueError:
            continue
        if not rel.startswith("sessions/"):
            return True
    return False
