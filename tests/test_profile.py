"""Profile saving sends only what the user changed (see dialogs.profile_changes)."""
from omnigram.dialogs import profile_changes

BEFORE = {"first_name": "Bob", "last_name": "Marley", "username": "bobomar", "about": "One love"}


def test_nothing_changed_sends_nothing():
    assert profile_changes(BEFORE, dict(BEFORE)) == {}


def test_the_bio_is_never_wiped_by_an_unrelated_edit():
    """The bug: the dialog opened with an empty Bio box and always sent it, erasing the real bio."""
    assert profile_changes(BEFORE, {**BEFORE, "first_name": "Robert"}) == {"first_name": "Robert"}


def test_an_unchanged_username_is_not_sent():
    """Telegram answers USERNAME_NOT_MODIFIED if the same username is set again; '@' and spaces don't count."""
    assert profile_changes(BEFORE, {**BEFORE, "username": " @bobomar "}) == {}


def test_clearing_a_field_is_a_change():
    assert profile_changes(BEFORE, {**BEFORE, "about": "", "username": ""}) == {"about": "", "username": ""}
