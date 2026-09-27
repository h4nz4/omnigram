"""Tests for omnigram.templates — template parsing and rendering.

Templates are short message bodies used by mailing/funnels/auto-replies.
Syntax:
  {name}, {username}, {first_name}, {last_name}, {phone} → per-recipient substitution
  {rand: a | b | c}                                 → one of the alternatives, picked fresh each render
  {spin: a | b | c}                                 → alias of {rand:...}
  Literal \\n                                       → newline
"""
from omnigram.templates import parse, render, validate


def test_render_substitutes_known_variables():
    assert render("Hi {first_name}!", {"first_name": "Anna"}) == "Hi Anna!"


def test_render_substitutes_multiple_variables():
    out = render("Hi {first_name} (@{username}), call {phone}",
                 {"first_name": "Bob", "username": "bob42", "phone": "+1234567890"})
    assert out == "Hi Bob (@bob42), call +1234567890"


def test_render_leaves_unknown_variables_intact():
    """An unknown variable stays as-is, so a typo never silently empties the message."""
    assert render("Hi {firstname}", {"first_name": "Anna"}) == "Hi {firstname}"


def test_render_rand_picks_one_alternative():
    """{rand: a | b | c} returns exactly one of the alternatives (whitespace trimmed)."""
    for _ in range(20):
        result = render("Pick: {rand: alpha | beta | gamma}")
        assert result in ("Pick: alpha", "Pick: beta", "Pick: gamma")


def test_render_spin_is_alias_for_rand():
    assert render("{spin: x | y}").strip() in ("x", "y")


def test_render_supports_literal_newline():
    assert render("line1\\nline2") == "line1\nline2"


def test_render_empty_template_is_empty():
    assert render("") == ""


def test_render_without_variables_returns_text():
    assert render("Just a plain message") == "Just a plain message"


def test_parse_extracts_variable_names():
    names = parse("Hi {first_name}, your @{username} and {phone}")
    assert names == {"first_name", "username", "phone"}


def test_parse_extracts_variables_inside_rand():
    names = parse("{rand: Hi {first_name} | Hello {first_name}}")
    assert "first_name" in names


def test_validate_complains_about_unbalanced_braces():
    errors = validate("Hi {first_name")
    assert any("brace" in e.lower() or "unbalanced" in e.lower() for e in errors)


def test_validate_complains_about_empty_rand():
    errors = validate("{rand:   }")
    assert errors


def test_validate_passes_for_clean_template():
    errors = validate("Hi {first_name}, {rand: hello | hi}")
    assert errors == []


def test_render_is_deterministic_per_call_with_seed():
    """Same seed produces same pick (so retries render identically)."""
    from random import Random
    r = Random(0)
    assert render("Pick: {rand: a | b | c}", seed=r) == "Pick: b"
