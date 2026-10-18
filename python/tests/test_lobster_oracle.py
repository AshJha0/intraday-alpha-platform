"""REL-12 external book oracle: per-level match rates and mismatch classes on a
synthetic LOBSTER fixture (lobster_fixture; no vendor data)."""

from __future__ import annotations

import pytest
from iap.marketdata.lobster_oracle import run_oracle
from lobster_fixture import write_lobster

DATE = "2012-06-21"


@pytest.mark.parametrize("resync", [False, True])
def test_a_matching_file_agrees_on_every_level_of_every_row(tmp_path, resync):
    msg, book, _ = write_lobster(tmp_path, "AAPL", DATE, 3, n_messages=500, levels=5)
    s = run_oracle(msg, book, DATE, resync=resync)
    assert s["rows"] == 500 and s["levels"] == 5
    assert s["rows_full_book_match"] == 500 and s["full_book_match_rate"] == 1.0
    assert s["match_rate_per_level_pooled"] == [1.0] * 5
    assert s["mismatch_level_rows_by_class"] == {
        "price": 0,
        "size": 0,
        "missing_level": 0,
        "extra_level": 0,
    }
    assert s["onsets_by_message_type"] == {} and s["rejected_events"] == 0
    assert s["resyncs"] == 0 and s["mode"] == ("resync" if resync else "free_running")


def _corrupt(book, row: int, col: int, delta: int):
    rows = book.read_text().splitlines()
    fields = rows[row].split(",")
    fields[col] = str(int(fields[col]) + delta)
    rows[row] = ",".join(fields)
    book.write_text("\n".join(rows) + "\n")
    return fields


def test_one_wrong_reference_row_is_one_size_mismatch_and_the_run_continues(tmp_path):
    msg, book, _ = write_lobster(tmp_path, "AAPL", DATE, 5, n_messages=300, levels=5)
    fields = _corrupt(book, 137, 3, +1)  # bid size, level 1, row 138
    s = run_oracle(msg, book, DATE)
    assert s["rows"] == 300  # never stops at the first divergence
    assert s["rows_full_book_match"] == 299
    assert s["mismatch_level_rows_by_class"]["size"] == 1
    assert sum(s["onsets_by_level"]["bid"]) == 1 and s["onsets_by_level"]["bid"][0] == 1
    assert sum(s["onsets_by_level"]["ask"]) == 0
    assert s["match_rate_per_level"]["bid"][0] == round(299 / 300, 6)
    assert s["match_rate_per_level"]["ask"] == [1.0] * 5
    (ex,) = [e for bucket in s["first_onset_examples_by_cause"].values() for e in bucket]
    assert (ex["row"], ex["side"], ex["level"], ex["class"]) == (138, "bid", 1, "size")
    assert ex["expected"] == [int(fields[2]), int(fields[3])]
    assert ex["actual"] == [int(fields[2]), int(fields[3]) - 1]


def test_a_wrong_price_is_a_price_mismatch(tmp_path):
    msg, book, _ = write_lobster(tmp_path, "AAPL", DATE, 6, n_messages=120, levels=5)
    _corrupt(book, 40, 0, -50)  # best ask price moved: a level we do not hold
    s = run_oracle(msg, book, DATE)
    assert s["mismatch_level_rows_by_class"]["price"] >= 1
    assert s["onsets_by_level"]["ask"][0] == 1


def test_resync_makes_each_row_a_one_message_test(tmp_path):
    """With resync the book is forced to the (wrong) row, so the error shows
    once more on the next row (the platform book now carries the extra share)
    and then the book is back in step: two onsets, not a persistent run."""
    msg, book, _ = write_lobster(tmp_path, "AAPL", DATE, 5, n_messages=300, levels=5)
    _corrupt(book, 137, 3, +1)
    s = run_oracle(msg, book, DATE, resync=True)
    assert s["resyncs"] == 2
    assert s["rows_full_book_match"] == 298
    assert s["onsets_by_level"]["bid"][0] == 2
