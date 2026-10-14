"""v1.16 item 12 (a): beta stability outside the fingerprinted alpha code."""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
from iap.agents.fingerprint import fingerprint
from iap.validation.beta_stability import beta_stability

SRC = Path(__file__).resolve().parents[1] / "src" / "iap"
NS_DAY = 86_400 * 1_000_000_000


def _frame(beta: float, days: int, n: int, seed: int) -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=days * n)
    y = beta * x + 0.1 * rng.normal(size=days * n)
    ts = np.repeat(np.arange(days) * NS_DAY, n) + np.tile(np.arange(n), days)
    df = pd.DataFrame({"exchange_ts": ts, "label_mid_1s": y, "label_valid_1s": True})
    return df, pd.Series(x)


def test_slopes_and_summary():
    items = [(i, *_frame(b, 3, 200, i)) for i, b in ((1, 2.0), (2, 2.0), (3, -1.0))]
    r = beta_stability(items, "label_mid_1s", "label_valid_1s")
    bi, bd = r["by_instrument"], r["by_instrument_day"]
    assert bi["n"] == 3 and bd["n"] == 9
    assert abs(bi["median"] - 2.0) < 0.05
    assert abs(bi["pos_frac"] - 2 / 3) < 1e-12
    assert abs(bi["sign_agreement"] - 2 / 3) < 1e-12
    assert beta_stability([], "a", "b")["by_instrument"]["n"] == 0


def test_short_slices_are_skipped():
    df, x = _frame(1.0, 1, 10, 0)  # 10 < 32 pairs
    assert beta_stability([(1, df, x)], "label_mid_1s", "label_valid_1s")["by_instrument"]["n"] == 0


def test_alpha_modules_do_not_import_it():
    for f in (SRC / "alpha").glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "beta_stability" not in node.module, f
    fp = fingerprint("EQ10")
    assert "iap.validation.beta_stability" not in fp["modules"]
