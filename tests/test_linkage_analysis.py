"""Tests for src/linkage_analysis.py using SYNTHETIC data only."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import linkage_analysis as la  # noqa: E402


def synthetic(n_dev=30, hours=6, seed=1):
    """SYNTHETIC: each fake device stays at its own fake cell every hour."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_dev):
        for h in range(hours):
            rows.append({
                la.TIME: 1000 + h, la.ENB: str(900000 + d), la.DEV: str(d),
                "province": "P%d" % (d % 3), "radio_access_type": "5G",
                "application_category": str(rng.choice(["A", "B"])),
            })
    return pd.DataFrame(rows)


def test_unique_devices_chain_fully():
    df = synthetic()
    _, u = la.unique_runs(df, [la.DEV])
    assert u.groupby("run").size().max() == 6
    assert la.chain_unicity(df, [la.DEV], samples=50)["4"] == 1.0


def test_shared_device_value_is_ambiguous():
    df = synthetic()
    df[la.DEV] = "same"  # all fake devices collapse into one value
    rep = la.link_report(df, 10)[0]
    assert rep["share_rows_unambiguous_in_hour"] == 0.0
    assert rep["chains_len_ge_2"] == 0


def test_equivalence_min_k():
    rep = la.equivalence_report(synthetic(), 10)
    assert rep[0]["min_k"] == 1 and rep[0]["share_rows_k1"] == 1.0
