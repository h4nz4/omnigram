"""Tests for omnigram.warmup_dialogs — the pure helpers behind the warm-up, dialogues,
online-keeper and randomizer dialogs. Only module-level functions are covered (no Qt)."""
from datetime import datetime, timezone
from random import Random

from omnigram.warmup import Action, schedule, warmup_plan
from omnigram.warmup_dialogs import (
    name_pairs,
    partner_config,
    plan_summary,
    split_names,
)


class FakePartner:
    """Stands in for an Account: only the fields partner_config reads."""

    def __init__(self, api_id: int = 0, api_hash: str = "", proxy: str = ""):
        self.api_id, self.api_hash, self.proxy = api_id, api_hash, proxy


# ---- plan_summary ---------------------------------------------------------------------------------

def test_plan_summary_empty_plan():
    assert plan_summary([]) == "no actions"


def test_plan_summary_mentions_days_and_when_it_ends():
    plan = [Action("read", day, datetime(2026, 6, 1 + day, 10 + i).timestamp()) for day in range(3) for i in range(4)]
    assert plan_summary(plan) == "12 actions over 3 day(s) (4–4/day) · ends Wed 03 Jun 13:00"


def test_plan_summary_shows_per_day_range_when_ramped():
    plan = schedule(warmup_plan(days=5, per_day=10, ramp=True, seed=Random(1)), datetime.now(timezone.utc), None)
    summary = plan_summary(plan)
    assert "(4–10/day)" in summary
    assert "5 day" in summary


# ---- partner_config -------------------------------------------------------------------------------

def test_partner_config_prefers_the_partner_own_credentials():
    partner = FakePartner(api_id=111, api_hash="ph", proxy="socks5://p:1")
    cfg = partner_config(partner, ("C:/s/partner.session", 999, "op"))
    assert cfg == {"session": "C:/s/partner.session", "api_id": 111, "api_hash": "ph", "proxy": "socks5://p:1"}


def test_partner_config_falls_back_to_the_operator_credentials():
    cfg = partner_config(FakePartner(), ("C:/s/partner.session", 999, "op"))
    assert cfg["api_id"] == 999 and cfg["api_hash"] == "op"


def test_partner_config_falls_back_per_field():
    partner = FakePartner(api_id=111)  # has an id but no hash
    cfg = partner_config(partner, ("C:/s/p.session", 999, "op"))
    assert cfg["api_id"] == 111 and cfg["api_hash"] == "op"


def test_partner_config_proxy_is_never_substituted():
    assert partner_config(FakePartner(), ("p", 1, "h"))["proxy"] == ""


# ---- split_names ----------------------------------------------------------------------------------

def test_split_names_one_per_line():
    assert split_names("Anna\nBob Smith\n") == ["Anna", "Bob Smith"]


def test_split_names_strips_and_drops_blanks():
    assert split_names("  Anna  \n\n   \nBob") == ["Anna", "Bob"]


def test_split_names_dedupes_keeping_order():
    assert split_names("Anna\nBob\nAnna\nBob\nCara") == ["Anna", "Bob", "Cara"]


def test_split_names_empty_text():
    assert split_names("") == []
    assert split_names("  \n \n") == []


# ---- name_pairs -----------------------------------------------------------------------------------

def test_name_pairs_splits_on_first_space():
    pairs = name_pairs({0: "Anna Maria Rossi", 1: "Bob"})
    assert pairs == [("Anna", "Maria Rossi"), ("Bob", "")]


def test_name_pairs_is_ordered_by_index():
    assert name_pairs({2: "C C", 0: "A"}) == [("A", ""), ("C", "C")]


def test_name_pairs_empty():
    assert name_pairs({}) == []
