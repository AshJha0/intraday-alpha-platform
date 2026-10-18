"""v1.17 E1: prices from the file, the spend ledger, the caps and the pins.

Mocked client only; every ledger is a tmp path (never the operator's).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from iap.llm import __main__ as llm_main
from iap.llm import prices, spend
from iap.llm.budget import (
    DEFAULT_MODEL,
    DEFAULT_PER_RUN_USD,
    EVAL_MODEL,
    PINNED_MODELS,
    PRICES,
    Budget,
    BudgetExceeded,
    pin_for,
    usage_cost,
)
from iap.llm.fake import Usage
from iap.llm.spend import MonthlyCapExceeded, SpendLedger, SpendPolicy

SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
PRICES_FILE = REPO_ROOT / "configs" / "reference" / "llm_prices.json"
CONFIG_FILE = REPO_ROOT / "configs" / "agents" / "llm.json"


def _u(i: int, o: int, read: int = 0, write: int = 0) -> Usage:
    return Usage(i, o, read, write)


# -- prices are configuration ------------------------------------------------


def test_prices_loaded_from_file():
    doc = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
    t = prices.load_prices()
    assert t.checked == doc["checked"] and len(t.checked) == 10 and t.source
    for m in (SONNET, HAIKU, "claude-opus-5-5"):
        assert m in t and m in PRICES
        assert PRICES[m] == (
            doc["models"][m]["input"],
            doc["models"][m]["output"],
            doc["models"][m]["cache_read"],
        )
    assert usage_cost(SONNET, _u(1_000_000, 0)) == pytest.approx(doc["models"][SONNET]["input"])
    assert usage_cost(HAIKU, _u(0, 1_000_000)) == pytest.approx(doc["models"][HAIKU]["output"])
    # cache read and write priced from the file's columns, not a hard-coded ratio
    assert usage_cost(SONNET, _u(0, 0, read=1_000_000)) == pytest.approx(
        doc["models"][SONNET]["cache_read"]
    )
    assert usage_cost(SONNET, _u(0, 0, write=1_000_000)) == pytest.approx(
        doc["models"][SONNET]["cache_write"]
    )


def test_no_price_hard_coded_in_code():
    src = (REPO_ROOT / "python" / "src" / "iap" / "llm").glob("*.py")
    for f in src:
        text = f.read_text(encoding="utf-8")
        assert "PRICES: dict" not in text and "(4.00, 20.00" not in text, f


def test_unknown_model_refused_never_guessed(tmp_path):
    with pytest.raises(prices.UnknownModelError, match="never guessed"):
        prices.usage_cost("claude-latest", _u(1, 1))
    with pytest.raises(prices.UnknownModelError):
        prices.max_call_cost("gpt-9", 10, 10)
    b = Budget(max_usd=100, unmetered=True)
    with pytest.raises(BudgetExceeded, match="no price"):
        b.check_model_call("claude-opus-9")
    assert "claude-opus-9" not in PRICES
    # a file that pins a floating alias is refused at load
    bad = tmp_path / "p.json"
    bad.write_text(
        json.dumps(
            {"checked": "2026-10-06", "models": {"claude-sonnet-latest": {"input": 1, "output": 2}}}
        )
    )
    with pytest.raises(ValueError, match="explicit"):
        prices.load_prices(bad)


def test_price_file_reloads_when_changed(tmp_path):
    p = tmp_path / "prices.json"
    p.write_text(
        json.dumps({"checked": "2026-01-01", "models": {"m": {"input": 1.0, "output": 2.0}}})
    )
    t1 = prices.load_prices(p)
    assert t1.price("m").cache_read == pytest.approx(0.1) and t1.price(
        "m"
    ).cache_write == pytest.approx(1.25)
    time.sleep(0.01)
    p.write_text(
        json.dumps({"checked": "2026-02-01", "models": {"m": {"input": 3.0, "output": 2.0}}})
    )
    os.utime(p, ns=(time.time_ns(), time.time_ns() + 10_000_000))
    assert prices.load_prices(p).price("m").input == 3.0


# -- dated pins -------------------------------------------------------------


def test_dated_pins_per_role():
    cfg = prices.load_llm_config()
    doc = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    assert (
        cfg.monthly_cap_usd == 30.0 and cfg.per_run_budget_usd == 1.0 and cfg.alert_fraction == 0.8
    )
    assert DEFAULT_PER_RUN_USD == 1.0 and Budget().max_usd == 1.0
    r = pin_for("research")
    assert r.as_dict() == {
        "model": SONNET,
        "pinned": doc["pins"]["research"]["pinned"],
        "prices_checked": prices.load_prices().checked,
    }
    assert pin_for("ops").model == HAIKU and pin_for("docs").model == HAIKU
    assert DEFAULT_MODEL == SONNET and EVAL_MODEL == HAIKU
    assert PINNED_MODELS["research"] == SONNET and "latest" not in "".join(PINNED_MODELS.values())
    # the most expensive model is no longer the default (E-7)
    assert PRICES[DEFAULT_MODEL][0] < max(PRICES[m][0] for m in PRICES)
    with pytest.raises(KeyError):
        cfg.pin("nope")


# -- the ledger -------------------------------------------------------------


def _ledger(
    tmp_path: Path, cap: float = 30.0, clock=None, notifier=None, frac: float = 0.8
) -> SpendLedger:
    return SpendLedger(
        tmp_path / "state" / "llm_spend.jsonl",
        policy=SpendPolicy(monthly_cap_usd=cap, per_run_budget_usd=1.0, alert_fraction=frac),
        clock=clock or (lambda: 1_792_000_000.0),  # 2026-10-14T...Z
        notifier=notifier or (lambda t, m: None),
    )


def test_default_path_outside_repo(monkeypatch, tmp_path):
    monkeypatch.delenv(spend.LEDGER_ENV, raising=False)
    p = spend.default_ledger_path()
    assert p.name == "llm_spend.jsonl" and p.parent.name == "iap"
    assert REPO_ROOT not in p.resolve().parents
    monkeypatch.setenv(spend.LEDGER_ENV, str(tmp_path / "x.jsonl"))
    assert spend.default_ledger_path() == tmp_path / "x.jsonl"
    with pytest.raises(ValueError, match="outside the repository"):
        SpendLedger(REPO_ROOT / "research" / "llm_spend.jsonl")


def test_recording_rows_and_month_to_date(tmp_path):
    led = _ledger(tmp_path)
    assert led.month_to_date() == 0.0 and not led.path.exists()
    pin = pin_for("research")
    row = led.record(
        model=SONNET,
        usage=_u(1000, 100, read=500, write=200),
        session="s1",
        run="r1",
        role="research",
        pin=pin,
    )
    assert row.utc.startswith("2026-10-14T") and row.month == "2026-10"
    assert row.tokens_in == 1000 and row.tokens_out == 100
    assert row.tokens_cache_read == 500 and row.tokens_cache_write == 200
    assert row.usd == pytest.approx(usage_cost(SONNET, _u(1000, 100, 500, 200)))
    assert row.month_usd == pytest.approx(row.usd)
    assert row.pinned == pin.pinned and row.prices_checked == pin.prices_checked
    led.record(model=HAIKU, usage=_u(10, 10), session="s2", run="r1", role="ops")
    lines = led.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    d = json.loads(lines[0])
    assert d["schema"] == spend.SCHEMA and d["session"] == "s1" and d["model"] == SONNET
    rows = led.last(20)
    assert [r.session for r in rows] == ["s1", "s2"]
    assert rows[1].month_usd == pytest.approx(rows[0].usd + rows[1].usd)
    assert led.month_to_date() == pytest.approx(rows[1].month_usd)
    assert not led.path.with_name(led.path.name + ".lock").exists()


def test_month_rollover(tmp_path):
    t = {"now": 1_790_000_000.0}  # 2026-09-21
    led = _ledger(tmp_path, clock=lambda: t["now"])
    led.record(model=SONNET, usage=_u(100_000, 10_000))
    sept = led.month_to_date()
    assert sept > 0 and led.month_to_date("2026-09") == pytest.approx(sept)
    t["now"] = 1_792_000_000.0  # 2026-10-14
    assert led.month_to_date() == 0.0
    r = led.record(model=SONNET, usage=_u(100, 10))
    assert r.month_usd == pytest.approx(r.usd)  # the new month starts from zero
    assert led.month_to_date("2026-09") == pytest.approx(sept)


def test_corrupt_rows_skipped_and_counted(tmp_path):
    led = _ledger(tmp_path)
    led.record(model=HAIKU, usage=_u(1_000_000, 0))  # $0.10
    with led.path.open("a", encoding="utf-8") as fh:
        fh.write('{"schema": "iap.llm.spend/1", "utc": "2026-10-14T00:00:00Z", "usd": "x"}\n')
        fh.write("not json\n")
        fh.write("[1, 2]\n")
        fh.write('{"schema": "other/1", "utc": "2026-10-14T00:00:00Z", "usd": 99}\n')
        fh.write('{"schema": "iap.llm.spend/1", "utc": "2026-10-14T00:00:00Z", "usd": 0.25')  # torn
    assert led.month_to_date() == pytest.approx(0.10)
    assert led.corrupt_rows == 5
    # a torn tail does not stop the next append
    led.record(model=HAIKU, usage=_u(1_000_000, 0))
    assert led.month_to_date() == pytest.approx(0.20)
    assert led.status()["corrupt_rows"] == 5


def test_hard_stop_refuses_before_the_call(tmp_path):
    led = _ledger(tmp_path, cap=1.0)
    led.record(model=SONNET, usage=_u(0, 99_000))  # $0.99
    est = prices.max_call_cost(SONNET, 1000, 4096)
    assert est > 0.01
    with pytest.raises(MonthlyCapExceeded, match="monthly cap"):
        led.check_call(SONNET, 1000, 4096)
    assert len(led.last()) == 1  # nothing recorded: the call was never made
    # estimate = prompt x input + max_tokens x output
    assert led.check_call(HAIKU, 1000, 4096) == pytest.approx((1000 * 0.10 + 4096 * 0.50) / 1e6)


def test_budget_obeys_ledger_cap_across_two_instances(tmp_path):
    """Two Budget instances (two sessions / processes) share one ledger: the
    second sees the first's spend on its very next call."""
    led_a = _ledger(tmp_path, cap=1.0)
    led_b = _ledger(tmp_path, cap=1.0)  # a second handle on the same file
    a = Budget(max_usd=5.0, ledger=led_a, session_id="A")
    b = Budget(max_usd=5.0, ledger=led_b, session_id="B")
    a.check_model_call(SONNET)
    a.charge(SONNET, _u(0, 60_000))  # $0.60
    b.check_model_call(SONNET)
    b.charge(SONNET, _u(0, 38_000))  # $0.38 -> $0.98 month-to-date; + 4096 out > $1
    with pytest.raises(BudgetExceeded, match="monthly cap"):
        a.check_model_call(SONNET)  # a's own usd is 0.60 < 5, the shared month says no
    with pytest.raises(BudgetExceeded, match="monthly cap"):
        b.check_model_call(SONNET)
    assert b.summary()["ledger"] == str(led_b.path) and a.summary()["usd"] == pytest.approx(0.60)
    rows = led_a.last()
    assert [r.session for r in rows] == ["A", "B"]


def test_alert_fires_once_per_month(tmp_path, caplog):
    calls: list[tuple[str, str]] = []
    t = {"now": 1_792_000_000.0}
    led = _ledger(
        tmp_path, cap=1.0, clock=lambda: t["now"], notifier=lambda a, m: calls.append((a, m))
    )
    led2 = _ledger(
        tmp_path, cap=1.0, clock=lambda: t["now"], notifier=lambda a, m: calls.append((a, m))
    )
    with caplog.at_level("WARNING", logger="iap.llm.spend"):
        led.record(model=SONNET, usage=_u(0, 70_000))  # $0.70 < 0.80
        assert calls == [] and not led.status()["alerted_this_month"]
        led.record(model=SONNET, usage=_u(0, 15_000))  # $0.85 >= 0.80
        assert len(calls) == 1 and "80%" in calls[0][1] and "$0.85" in calls[0][1]
        assert any("monthly cap" in r.message for r in caplog.records)
        led2.record(model=SONNET, usage=_u(0, 1_000))  # another process: no second toast
        led.record(model=SONNET, usage=_u(0, 1_000))
        assert len(calls) == 1 and led2.status()["alerted_this_month"]
        t["now"] += 31 * 86400  # next month: fires again once
        led.record(model=SONNET, usage=_u(0, 85_000))
        assert len(calls) == 2


def test_budget_without_ledger_refused_unless_unmetered(tmp_path):
    with pytest.raises(BudgetExceeded, match="no spend ledger"):
        Budget(max_usd=100).check_model_call(SONNET)
    b = Budget(max_usd=0.001, unmetered=True)
    with pytest.raises(BudgetExceeded, match="usd cap"):
        b.check_model_call(SONNET)
    assert b.summary()["ledger"] is None and b.summary()["pin"] is None


def test_spend_cli(tmp_path, capsys, monkeypatch):
    led = _ledger(tmp_path)
    for i in range(25):
        led.record(model=HAIKU, usage=_u(100, 10), session=f"s{i}", run="r")
    monkeypatch.setenv(spend.LEDGER_ENV, str(led.path))
    assert llm_main.main(["spend"]) == 0
    out = capsys.readouterr().out
    assert "month-to-date" in out and "cap $30.00" in out and "per-run $1.00" in out
    assert "last 20 of 25 rows" in out and "session=s24" in out and "session=s4" not in out
    assert llm_main.main(["spend", "--json", "--last", "2"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["monthly_cap_usd"] == 30.0 and len(d["last"]) == 2 and d["rows"] == 25
    assert d["remaining_usd"] == pytest.approx(30.0 - d["month_to_date_usd"])


def test_run_session_attaches_ledger_to_non_scripted_client(tmp_path, monkeypatch):
    from iap.llm import agent as ag
    from iap.llm.fake import ScriptedClient

    led = _ledger(tmp_path)
    monkeypatch.setattr(spend.SpendLedger, "default", classmethod(lambda cls: led))

    class _Sess:
        session_id = "s1"

        def __init__(self):
            self.budget = Budget()

    class _OtherClient:  # e.g. a wrapper around a real SDK client
        pass

    s1, s2 = _Sess(), _Sess()
    ag._wire_spend(_OtherClient(), s1, SONNET)
    assert s1.budget.ledger is led and not s1.budget.unmetered
    ag._wire_spend(ScriptedClient([]), s2, SONNET)
    assert s2.budget.ledger is None and s2.budget.unmetered
