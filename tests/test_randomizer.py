"""Tests for omnigram.randomizer and omnigram.phones — account data randomizer + number checker."""
from random import Random

import pytest

from omnigram.phones import check_list, normalize_number, to_phone
from omnigram.randomizer import assign_names, random_bio, random_name, random_profile

NAMES = ["Anna Smith", "Bob Jones", "Cara Lee", "Dan Reed", "Eve Cole"]


def test_random_name_uses_the_supplied_list():
    for _ in range(10):
        assert random_name(NAMES) in NAMES


def test_random_name_empty_list_raises():
    with pytest.raises(ValueError):
        random_name([])


def test_random_name_is_deterministic_with_seed():
    assert random_name(NAMES, seed=Random(4)) == random_name(NAMES, seed=Random(4))


def test_random_bio_is_assembled_from_parts():
    bio = random_bio(seed=Random(0))
    assert isinstance(bio, str) and bio


def test_random_profile_has_name_and_bio():
    profile = random_profile(NAMES, seed=Random(1))
    assert profile["first_name"] and profile["last_name"]
    assert profile["about"]


def test_assign_names_gives_everyone_a_unique_name_when_possible():
    assigned = assign_names(5, NAMES, seed=Random(2))
    assert len(assigned) == 5
    assert len(set(assigned.values())) == 5  # no duplicates
    assert all(name in NAMES for name in assigned.values())


def test_assign_names_reuses_when_fewer_names_than_accounts():
    assigned = assign_names(8, NAMES, seed=Random(3))
    assert len(assigned) == 8
    assert set(assigned.values()) <= set(NAMES)


def test_assign_names_keys_are_indexes():
    assert sorted(assign_names(3, NAMES, seed=Random(0))) == [0, 1, 2]


# ---- phones ---------------------------------------------------------------------------------

def test_normalize_number_keeps_digits_and_leading_plus():
    assert normalize_number("+1 (234) 567-890") == "+1234567890"
    assert normalize_number("1234567890") == "+1234567890"
    assert normalize_number("") is None


def test_normalize_number_rejects_garbage():
    assert normalize_number("not a number") is None
    assert normalize_number("+") is None


def test_to_phone_marks_valid_numbers():
    valid = to_phone("+12025550143")  # real format, US test range
    assert valid and valid.startswith("+")


def test_check_list_dedupes_and_normalizes():
    out = check_list(["+12025550143", "+1 202 555 0143", "garbage", ""])
    assert len(out) == 1
    assert out[0].endswith("2025550143")


def test_check_list_keeps_order():
    out = check_list(["+12025550143", "+442071838750"])
    assert out[0].startswith("+1")
    assert out[1].startswith("+44")
