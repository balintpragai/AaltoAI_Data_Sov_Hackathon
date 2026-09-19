"""Linkage analysis for the anonymised telemetry file.

Measures what record-to-record linkage survives anonymisation when there is
no stable subscriber pseudonym. Outputs AGGREGATES ONLY (no identifier or
cell values), as JSON.

Run by a human in the controlled environment (AGENTS.md 1.4):
    python src/linkage_analysis.py --input anonymized_output.csv --out linkage_report.json

Metrics (WP216 tests; ISO/IEC 20889; NIST SP 800-188):
  1. equivalence classes  - k-anonymity of quasi-identifier sets (singling out)
  2. link ambiguity       - how many candidate rows a record has in the next hour
  3. reconstructed chains - runs of hours where a key is unambiguous (pseudo-sessions)
  4. chain unicity        - p random (hour, enb) points identify a chain (de Montjoye 2013)
"""
import argparse
import json
import random
from collections import defaultdict

import pandas as pd

TIME, ENB, DEV = "time_start", "enb_id", "imei"
COLS = [TIME, ENB, DEV, "province", "radio_access_type", "application_category"]

# Quasi-identifier sets for equivalence-class (k-anonymity) analysis.
QI_SETS = [
    [TIME, ENB],
    [TIME, ENB, "radio_access_type"],
    [TIME, ENB, "application_category"],
    [TIME, ENB, DEV],
    [TIME, ENB, DEV, "radio_access_type", "application_category"],
    [TIME, "province", DEV],
]

# Candidate linking keys: rows sharing the key in consecutive hours may be one subject.
LINK_KEYS = [
    [DEV],
    [DEV, "radio_access_type"],
    [DEV, "province", "radio_access_type"],
    [DEV, ENB],
    [DEV, ENB, "application_category"],
]


def load(path, nrows=None):
    df = pd.read_csv(path, usecols=COLS, nrows=nrows, dtype=str)
    t = pd.to_numeric(df[TIME], errors="coerce")
    if t.isna().any():  # timestamps -> integer hours
        t = pd.to_datetime(df[TIME], errors="coerce").dt.floor("h").astype("int64") // 3_600_000_000_000
    df[TIME] = t.astype("int64")
    return df


def equivalence_report(df, k, violations_path=None):
    """Also lists rows in classes smaller than k as (row_number, qi_set, class_size)
    -- positions and sizes only, never values. row_number = CSV line number."""
    out = []
    first = True
    for cols in QI_SETS:
        sizes = df.groupby(cols, dropna=False)[TIME].transform("size")
        bad = sizes[sizes < k]
        if violations_path is not None:
            v = pd.DataFrame({"csv_line": bad.index + 2, "qi_set": "+".join(cols), "class_size": bad.values})
            v.to_csv(violations_path, mode="w" if first else "a", header=first, index=False)
            first = False
        out.append({
            "qi": cols,
            "classes": int(df.groupby(cols, dropna=False).ngroups),
            "min_k": int(sizes.min()),
            "share_rows_k1": round(float((sizes == 1).mean()), 4),
            f"share_rows_k_lt_{k}": round(float((sizes < k).mean()), 4),
            "median_k": float(sizes.median()),
            "violating_rows": int(len(bad)),
            "first_violating_csv_lines": [int(i) + 2 for i in bad.index[:100]],
        })
    return out


def unique_runs(df, key):
    """Rows whose key occurs once in their hour, grouped into runs of consecutive
    hours (= reconstructed pseudo-sessions). Returns all rows with per-hour count
    and the unambiguous rows with a run id."""
    d = df[list(dict.fromkeys(key + [TIME, ENB]))].copy()
    d["_n"] = d.groupby(key + [TIME], dropna=False)[TIME].transform("size")
    u = d[d["_n"] == 1].sort_values(key + [TIME])
    new_run = (u[TIME].diff() != 1) | u[key].ne(u[key].shift()).any(axis=1)
    return d, u.assign(run=new_run.cumsum())


def link_report(df, k):
    out = []
    for key in LINK_KEYS:
        d, u = unique_runs(df, key)
        counts = d.groupby(key + [TIME], dropna=False).size().rename("n").reset_index()
        nxt = counts.assign(**{TIME: counts[TIME] - 1}).rename(columns={"n": "n_next"})
        m = counts.merge(nxt, on=key + [TIME], how="left")
        has_next = m["n_next"].notna()
        one_to_one = (m["n"] == 1) & (m["n_next"] == 1)
        runs = u.groupby("run").size()
        long_runs = runs[runs >= 2]
        out.append({
            "key": key,
            "share_rows_unambiguous_in_hour": round(float((d["_n"] == 1).mean()), 4),
            "share_key_hours_with_next_hour_candidates": round(float(has_next.mean()), 4),
            "share_key_hours_1to1_link_next_hour": round(float(one_to_one.mean()), 4),
            "median_candidates_next_hour": float(m["n_next"].median()) if has_next.any() else None,
            "chains_total": int(len(runs)),
            "chains_len_1": int((runs == 1).sum()),
            "chains_len_ge_2": int(len(long_runs)),
            "rows_in_chains_len_ge_2": round(float(long_runs.sum() / len(df)), 4),
            "chain_len_histogram": {str(int(i)): int(c) for i, c in runs.value_counts().sort_index().head(24).items()},
            "chain_len_max": int(runs.max()) if len(runs) else 0,
            "chain_len_p50_p90": [float(runs.quantile(.5)), float(runs.quantile(.9))] if len(runs) else None,
        })
    return out


def chain_unicity(df, key, ps=(1, 2, 3, 4), samples=500, seed=0):
    """For chains of length >= p (length-1 chains included for p=1): pick p random (hour, enb) points from a chain and
    count how many chains contain all of them. Reports the share matched by exactly one chain."""
    _, u = unique_runs(df, key)
    chains = {rid: set(zip(g[TIME], g[ENB])) for rid, g in u.groupby("run") if len(g) >= 1}
    if not chains:
        return {str(p): None for p in ps}  # no row is unambiguous under this key
    index = defaultdict(set)
    for rid, pts in chains.items():
        for pt in pts:
            index[pt].add(rid)
    rng = random.Random(seed)
    ids = list(chains)
    res = {}
    for p in ps:
        eligible = [r for r in ids if len(chains[r]) >= p]
        if not eligible:
            res[str(p)] = None
            continue
        picks = rng.sample(eligible, min(samples, len(eligible)))
        hit = 0
        for r in picks:
            pts = rng.sample(sorted(chains[r]), p)
            hit += len(set.intersection(*(index[pt] for pt in pts))) == 1
        res[str(p)] = round(hit / len(picks), 4)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", default="linkage_report.json")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--samples", type=int, default=500)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--violations-out", default="linkage_violations.csv",
                    help="row positions/class sizes for k<threshold (no values); keep out of git")
    ap.add_argument("--nrows", type=int, default=None, help="limit rows (testing only)")
    a = ap.parse_args()

    df = load(a.input, a.nrows)
    report = {
        "n_rows": int(len(df)),
        "n_hours": int(df[TIME].nunique()),
        "k_threshold": a.k,
        "distinct_enb": int(df[ENB].nunique()),
        "distinct_device_values": int(df[DEV].nunique()),
        "equivalence_classes": equivalence_report(df, a.k, a.violations_out),
        "linking": link_report(df, a.k),
        "chain_unicity_share_unique": {
            "+".join(k): chain_unicity(df, k, samples=a.samples, seed=a.seed) for k in LINK_KEYS
        },
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"wrote aggregate report to {a.out}")


if __name__ == "__main__":
    main()
