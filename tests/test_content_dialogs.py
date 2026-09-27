from omnigram.content_dialogs import parse_ids


def test_ranges_singles_and_garbage():
    assert parse_ids("1-5, 8, 12") == [1, 2, 3, 4, 5, 8, 12]


def test_dedupe_and_sort():
    assert parse_ids("12, 3-5, 4, 1, 5") == [1, 3, 4, 5, 12]


def test_ignores_garbage():
    assert parse_ids("hello, 7, , abc-3, 2-2") == [2, 7]


def test_rejects_negatives():
    assert parse_ids("-3, -1-2, 4") == [4]


def test_empty_and_whitespace():
    assert parse_ids("") == []
    assert parse_ids("  ,  ;  \n ") == []


def test_whitespace_and_semicolons():
    assert parse_ids(" 1 - 3 ; 9\n10 ") == [1, 2, 3, 9, 10]


def test_reversed_range_is_normalized():
    assert parse_ids("5-2") == [2, 3, 4, 5]