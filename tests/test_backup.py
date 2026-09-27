from omnigram.backup import export_backup, import_backup
from omnigram.store import Store


def test_export_import_roundtrip(tmp_path):
    src = Store(tmp_path / "src")
    (src.sessions / "a.session").write_bytes(b"key-a")
    (src.sessions / "b.session").write_bytes(b"key-b")
    a, b = src.load()
    a.name, a.phone = "Alice", "123"
    src.save([a, b])

    zip_path = tmp_path / "backup.zip"
    export_backup(src, [a], zip_path)  # only back up 'a'

    dest = Store(tmp_path / "dest")
    (dest.sessions / "a.session").write_bytes(b"already-here")  # pre-existing -> must be skipped, not overwritten
    imported, skipped = import_backup(dest, zip_path)
    assert imported == [] and skipped == ["a"]
    assert (dest.sessions / "a.session").read_bytes() == b"already-here"

    dest2 = Store(tmp_path / "dest2")
    imported, skipped = import_backup(dest2, zip_path)
    assert imported == ["a"] and skipped == []
    [restored] = dest2.load()
    assert (restored.session, restored.name, restored.phone) == ("a", "Alice", "123")
    assert (dest2.sessions / "a.session").read_bytes() == b"key-a"
