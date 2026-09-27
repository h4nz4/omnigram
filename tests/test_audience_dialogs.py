"""Tests for omnigram.audience_dialogs — the pure helpers behind the parser/funnel/number dialogs.

The dialogs themselves are Qt and untested here; everything decision-shaped (which funnel steps are
due, how results are rendered, how a pasted list is split) is a module-level function and covered.
"""
from datetime import datetime, timedelta

from omnigram.audience_dialogs import (
    add_subscribers,
    due_steps,
    parse_steps,
    recipient_for,
    recipients_csv,
    results_text,
    rows_csv,
    split_numbers,
    steps_from_table,
    usernames_text,
)
from omnigram.broadcast import Recipient
from omnigram.funnel import Funnel, Step, advance, new_entry

T0 = datetime(2026, 1, 1, 12, 0)


def funnel(*delays):
    return Funnel(name="test", steps=[Step(hours=h, template=f"msg{h}") for h in delays])


# ---- due_steps -----------------------------------------------------------------------------------

def test_due_steps_returns_only_due_entries():
    f = funnel(2, 24)
    entries = {1: new_entry(T0, f), 2: new_entry(T0, f)}
    entries[1]["due_at"] = T0 - timedelta(hours=1)  # forced due
    assert due_steps(f, entries, T0) == [(1, "msg2")]


def test_due_steps_renders_the_current_step_template():
    f = Funnel(name="t", steps=[Step(0, "first {first_name}"), Step(0, "second")])
    entry = new_entry(T0, f)
    assert due_steps(f, {1: entry}, T0) == [(1, "first {first_name}")]  # no context: variable intact
    advance(entry, f, T0)
    assert due_steps(f, {1: entry}, T0) == [(1, "second")]


def test_due_steps_keeps_username_keys():
    f = Funnel(name="t", steps=[Step(0, "hi")])
    assert due_steps(f, {"ann": new_entry(T0, f)}, T0) == [("ann", "hi")]


def test_due_steps_empty_when_nothing_due():
    f = funnel(5)
    assert due_steps(f, {1: new_entry(T0, f)}, T0) == []


def test_recipient_for_numeric_key_and_username():
    assert recipient_for(42) == Recipient(id=42)
    assert recipient_for("ann") == Recipient(id=0, username="ann")


# ---- recipients_csv ------------------------------------------------------------------------------

def test_recipients_csv_header_and_row():
    r = Recipient(id=1, first_name="Ann", last_name="Smith", username="ann", phone="+1202")
    lines = recipients_csv([r]).strip().splitlines()
    assert lines[0] == "id,first_name,last_name,username,phone"
    assert lines[1] == "1,Ann,Smith,ann,+1202"


def test_recipients_csv_quotes_commas_and_quotes():
    r = Recipient(id=2, first_name='Ann "A", Jr')
    assert '"Ann ""A"", Jr"' in recipients_csv([r]).splitlines()[1]


def test_recipients_csv_empty_has_only_header():
    assert recipients_csv([]).strip() == "id,first_name,last_name,username,phone"


# ---- usernames_text ------------------------------------------------------------------------------

def test_usernames_text_one_per_line():
    assert usernames_text([Recipient(id=1, username="ann"), Recipient(id=2, username="bob")]) == "@ann\n@bob"


def test_usernames_text_falls_back_to_numeric_id():
    assert usernames_text([Recipient(id=5)]) == "5"


def test_usernames_text_empty():
    assert usernames_text([]) == ""


# ---- results_text / rows_csv ---------------------------------------------------------------------

def test_results_text_marks_registered_and_shows_name():
    rows = [{"phone": "+1", "registered": True, "name": "Ann Smith", "username": "ann"},
            {"phone": "+2", "registered": False}]
    lines = results_text(rows).splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("+1") and "✓" in lines[0] and "Ann Smith" in lines[0] and "@ann" in lines[0]
    assert lines[1].startswith("+2") and "✗" in lines[1] and "Ann Smith" not in lines[1]


def test_results_text_marks_retry():
    assert "(retry)" in results_text([{"phone": "+1", "registered": False, "retry": True}])


def test_rows_csv_header_and_values():
    rows = [{"phone": "+1", "registered": True, "id": 42, "username": "ann", "name": "Ann", "retry": False}]
    lines = rows_csv(rows).strip().splitlines()
    assert lines[0] == "phone,registered,id,username,name,retry"
    assert lines[1] == "+1,True,42,ann,Ann,False"


def test_rows_csv_missing_keys_use_schema_defaults():
    assert rows_csv([{"phone": "+1"}]).strip().splitlines()[1] == "+1,False,0,,,False"


# ---- parsing pasted input ------------------------------------------------------------------------

def test_parse_steps_reads_hours_and_template():
    steps = parse_steps("0 | Hi {first_name}\n24: Follow up\n48\tLast")
    assert [(s.hours, s.template) for s in steps] == [(0.0, "Hi {first_name}"), (24.0, "Follow up"), (48.0, "Last")]


def test_parse_steps_skips_blank_comments_and_garbage():
    assert parse_steps("\n# note\nno separator\nabc | x\n2 | ok") == [Step(2.0, "ok")]


def test_steps_from_table_skips_blank_templates():
    rows = [("2", "hi"), ("", ""), ("x", "kept at zero")]
    assert steps_from_table(rows) == [Step(2.0, "hi"), Step(0.0, "kept at zero")]


def test_split_numbers_handles_lines_commas_and_semicolons():
    assert split_numbers("+1, +2\n+3\n\n;+4") == ["+1", "+2", "+3", "+4"]


def test_add_subscribers_keys_by_id_or_username_and_keeps_progress():
    f = funnel(0, 24)
    entries = {7: new_entry(T0, f)}
    entries[7]["step"] = 1  # pretend it already moved on
    added = add_subscribers(entries, "@ann\n999\n@ann\n7", f, T0)
    assert added == 2  # @ann and 999 are new; 7 already present, and @ann repeats
    assert set(entries) == {7, "ann", 999}
    assert entries[7]["step"] == 1
    assert entries["ann"]["due_at"] == T0  # first step is 0 h
