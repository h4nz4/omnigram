"""Tests for omnigram.template_store — named templates persisted to templates.json."""
from omnigram.template_store import TemplateStore


def test_load_missing_file_is_empty(tmp_path):
    assert TemplateStore(tmp_path / "templates.json").load() == {}


def test_upsert_and_load_roundtrip(tmp_path):
    store = TemplateStore(tmp_path / "templates.json")
    store.upsert("hello", "Hi {first_name}!")
    assert store.load() == {"hello": "Hi {first_name}!"}


def test_upsert_replaces_existing(tmp_path):
    store = TemplateStore(tmp_path / "templates.json")
    store.upsert("a", "one")
    store.upsert("a", "two")
    assert store.load() == {"a": "two"}


def test_remove(tmp_path):
    store = TemplateStore(tmp_path / "templates.json")
    store.upsert("a", "one")
    store.remove("a")
    assert store.load() == {}


def test_remove_missing_is_noop(tmp_path):
    store = TemplateStore(tmp_path / "templates.json")
    store.remove("nope")
    assert store.load() == {}


def test_store_in_place_of_a_garbage_file(tmp_path):
    path = tmp_path / "templates.json"
    path.write_text("{not json", "utf-8")
    assert TemplateStore(path).load() == {}  # a corrupt file must not take the app down


def test_names_are_sorted(tmp_path):
    store = TemplateStore(tmp_path / "templates.json")
    for name in ("zeta", "alpha", "mid"):
        store.upsert(name, name)
    assert list(store.load()) == ["alpha", "mid", "zeta"]


def test_save_is_atomic_and_survives_reload(tmp_path):
    path = tmp_path / "templates.json"
    TemplateStore(path).upsert("x", "y")
    assert TemplateStore(path).load() == {"x": "y"}
