"""Economic evaluation of signals: hand-verified cases (spec §14)."""

from __future__ import annotations

import numpy as np
import pytest

from iap.models.economics import (
    realized_net,
    signal_directions,
    signal_economics,
)


def test_signal_directions_hand():
    pred = np.array([0.0005, -0.0001, -0.0009, 0.0])
    cost = np.array([0.0002, 0.0002, 0.0002, 0.0002])
    d = signal_directions(pred, cost)
    # long: pred > 0.  short: -pred - 2c > 0 -> pred < -2c = -0.0004.
    assert d.tolist() == [1, 0, -1, 0]


def test_signal_directions_threshold():
    pred = np.array([0.0003, 0.0007])
    cost = np.zeros(2)
    d = signal_directions(pred, cost, threshold=0.0005)
    assert d.tolist() == [0, 1]


def test_realized_net_hand():
    # buy: net = y_cost ; sell: net = y_cost - 2*y_mid (cost >= 0 case)
    y_mid = np.array([0.001, 0.001])
    y_cost = np.array([0.0006, 0.0006])  # cost = 4bp
    net = realized_net(np.array([1, -1]), y_mid, y_cost)
    assert abs(net[0] - 0.0006) < 1e-15
    assert abs(net[1] - (0.0006 - 0.002)) < 1e-15


def test_realized_net_conservative_floors_negative_cost():
    # crossed book: y_cost > y_mid  =>  raw cost negative
    y_mid = np.array([0.0])
    y_cost = np.array([0.002])  # "paid" 20bp to trade
    d = np.array([1])
    exact = realized_net(d, y_mid, y_cost, conservative=False)
    cons = realized_net(d, y_mid, y_cost, conservative=True)
    assert abs(exact[0] - 0.002) < 1e-15  # synthetic arb captured
    assert cons[0] == 0.0                  # floored away


def test_signal_economics_hand_case():
    pred = np.array([0.001, -0.002, 0.0])
    cost_est = np.array([0.0002, 0.0002, 0.0002])
    y_mid = np.array([0.0008, -0.0010, 0.0002])
    y_cost = np.array([0.0004, -0.0014, -0.0001])
    econ = signal_economics(pred, y_mid, y_cost, cost_est)
    # directions: [1, -1, 0]
    assert econ["n_trades"] == 2
    assert econ["n_long"] == 1 and econ["n_short"] == 1
    # trade 1 net = y_cost = 4bp; trade 2 (sell): +0.0010 - 0.0004 = 6bp
    assert abs(econ["total_net_bps"] - 10.0) < 1e-9
    assert abs(econ["mean_net_bps_per_signal"] - 5.0) < 1e-9
    assert econ["hit_rate"] == 1.0
    assert econ["n_samples"] == 3


def test_signal_economics_no_trades():
    econ = signal_economics(np.zeros(5), np.zeros(5), np.zeros(5),
                            np.full(5, 0.001))
    assert econ["n_trades"] == 0
    assert econ["mean_net_bps_per_signal"] == 0.0


def test_shape_validation():
    with pytest.raises(ValueError):
        signal_directions(np.zeros(3), np.zeros(4))
