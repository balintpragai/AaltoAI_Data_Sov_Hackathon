#!/usr/bin/env python3
"""
attack_baseline.py - scripted re-identification baseline for guide_anonymize.py

Works on SYNTHETIC data only, because scoring needs ground truth.

Workflow
--------
1. Add ground-truth columns to the raw synthetic CSV (they pass through the anonymiser
   untouched because it only changes listed columns):
       python attack_baseline.py --add-gt raw.csv raw_with_gt.csv
   -> adds gt_user (= msisdn) and gt_row_id (= original row number)

2. Run the anonymiser on raw_with_gt.csv  ->  outputs/anon_with_gt.csv

3. Run the attacks:
       python attack_baseline.py --raw raw_with_gt.csv --anon outputs/anon_with_gt.csv \
              --k-min 10 --sig-figs 3 --tz-offset 2 --report attack_report.json

   Quick check that the harness itself works (builds synthetic data, applies a
   reference re-implementation of the documented pipeline, then attacks it):
       python attack_baseline.py --selftest

The attacks only use what an attacker would see (anon columns except gt_*). The gt_*
columns are used exclusively for SCORING. Give an AI agent a copy of the anon file with
gt_user and gt_row_id dropped.

Requires: pandas, numpy. Optional: scikit-learn (app-category recovery attack).
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

GT_USER, GT_ROW = "gt_user", "gt_row_id"
QI = ["time_start", "province", "radio_access_type", "enb_id", "application_category"]
KPI = [
    "data_GB_sum", "im_video_GB_sum", "im_audio_GB_sum", "tethering_data_GB_dl_sum",
    "tp_dl_avg", "tp_ul_avg", "tp_dl_filtered_avg", "cont_rtt_radio_avg",
    "cont_rtt_internet_avg", "initial_rtt_radio_avg",
    "tcp_retrans_byte_ratio_downlink_avg", "tcp_retrans_byte_ratio_uplink_avg",
    "http_response_time_avg", "http_sr_avg",
]
# KPIs that tend to be stable per user/device (used for row linking; volumes excluded)
LINK = [
    "tp_dl_avg", "tp_ul_avg", "tp_dl_filtered_avg", "cont_rtt_radio_avg",
    "cont_rtt_internet_avg", "initial_rtt_radio_avg",
    "tcp_retrans_byte_ratio_downlink_avg", "tcp_retrans_byte_ratio_uplink_avg",
    "http_response_time_avg", "http_sr_avg",
]


# --------------------------------------------------------------------------- helpers
def read(path):
    return pd.read_csv(
        path,
        dtype={"imei": "string", "msisdn": "string", "imsi": "string", GT_USER: "string"},
        low_memory=False,
    )


def cols(df, names):
    return [c for c in names if c in df.columns]


def sig_round(x, s):
    x = np.asarray(x, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        mag = np.floor(np.log10(np.abs(x)))
    mag = np.where(np.isfinite(mag), mag, 0)
    f = 10.0 ** (s - 1 - mag)
    return np.where(np.isfinite(x), np.round(x * f) / f, x)


def add_gt(src, dst):
    df = read(src)
    if "msisdn" not in df.columns:
        sys.exit("msisdn column required to derive gt_user")
    df[GT_USER] = df["msisdn"]
    df[GT_ROW] = np.arange(len(df))
    df.to_csv(dst, index=False)
    print(f"wrote {dst} ({len(df)} rows) with {GT_USER}, {GT_ROW}")


def _time_feats(hours, tz):
    ts = pd.to_datetime((hours.astype("int64") + tz) * 3600, unit="s")
    return ts.dt.hour.to_numpy(), ts.dt.dayofweek.to_numpy()


def prep_anon(anon, tz):
    a = anon.reset_index(drop=True).copy()
    if "imei" in a.columns:
        a["tac"] = a["imei"].astype("string").str.strip().fillna("NA").astype(str)
    else:
        a["tac"] = "NA"
    a["enb_s"] = a["enb_id"].astype(str).str.replace(r"\.0$", "", regex=True)
    a["enb_supp"] = a["enb_s"].eq("0")
    a["app_supp"] = a["application_category"].astype(str).eq("RESTRICTED")
    a["time_start"] = pd.to_numeric(a["time_start"]).astype("int64")
    a["hod"], a["dow"] = _time_feats(a["time_start"], tz)
    return a


def prep_raw(raw, tz):
    r = raw.reset_index(drop=True).copy()
    r["tac"] = r["imei"].astype("string").str[:8].fillna("NA").astype(str)
    r["enb_s"] = r["enb_id"].astype(str).str.replace(r"\.0$", "", regex=True)
    r["hour"] = (pd.to_numeric(r["time_start"]) // 3600).astype("int64")
    r["hod"], r["dow"] = _time_feats(r["hour"], tz)
    return r


def true_anchors(r, night=(0, 5), work=(9, 16)):
    def mode(d):
        return d.groupby(GT_USER)["enb_s"].agg(lambda s: s.mode().iat[0])
    return (
        mode(r[r.hod.between(*night)]),
        mode(r[(r.dow < 5) & r.hod.between(*work)]),
    )


# --------------------------------------------------------------------------- checks
def check_format(a, sig):
    out = {"direct_id_columns_present": [c for c in ("msisdn", "imsi") if c in a.columns]}
    out["index_like_columns"] = [
        c for c in a.columns
        if c.lower().startswith("unnamed") or c.lower() in ("index", "id", "row_id")
    ]
    if "imei" in a.columns:
        ln = a["imei"].dropna().astype(str).str.len().value_counts().head(5)
        out["imei_length_distribution"] = {str(k): int(v) for k, v in ln.items()}
    u = pd.factorize(a[GT_USER])[0]
    adj = float((u[1:] == u[:-1]).mean())
    p = a[GT_USER].value_counts(normalize=True)
    exp = float((p ** 2).sum())
    out["row_order"] = {
        "adjacent_rows_same_user": adj,
        "expected_if_shuffled": exp,
        "LEAK": bool(adj > max(3 * exp, 0.01)),
        "time_sorted_share": float((np.diff(a["time_start"].to_numpy()) >= 0).mean()),
    }
    excess = {}
    for c in cols(a, KPI):
        x = pd.to_numeric(a[c], errors="coerce").dropna()
        x = x[x != 0].to_numpy()
        if len(x):
            excess[c] = float((np.abs(sig_round(x, sig) - x) > 1e-9 * np.abs(x)).mean())
    out["values_with_more_than_sigfigs_precision"] = excess
    return out


def check_k(a, k):
    qi = cols(a, QI)
    g = a.groupby(qi, dropna=False).agg(rows=(GT_USER, "size"), users=(GT_USER, "nunique"))
    return {
        "classes": int(len(g)),
        "k_min": k,
        "share_classes_with_users_lt_k": float((g.users < k).mean()),
        "share_rows_in_classes_with_users_lt_k": float(g.loc[g.users < k, "rows"].sum() / len(a)),
        "share_rows_in_single_user_classes": float(g.loc[g.users == 1, "rows"].sum() / len(a)),
        "median_rows_per_user_in_class": float((g.rows / g.users).median()),
    }


def check_tac(a):
    tu = a.groupby("tac")[GT_USER].nunique()
    per_row = a["tac"].map(tu)
    n_users = a[GT_USER].nunique()
    out = {"distinct_tacs": int(len(tu)), "distinct_users": int(n_users)}
    for m in (1, 3, 5, 10):
        sel = per_row <= m
        out[f"rows_with_tac_shared_by_le_{m}_users"] = float(sel.mean())
        out[f"users_behind_such_tacs_le_{m}"] = float(a.loc[sel, GT_USER].nunique() / n_users)
    return out


def check_uniqueness(a):
    qi, kpi = cols(a, QI), cols(a, KPI)
    sets = {
        "QI": qi, "QI+TAC": qi + ["tac"], "KPI_tuple": kpi,
        "TAC+KPI_tuple": ["tac"] + kpi, "QI+TAC+KPI_tuple": qi + ["tac"] + kpi,
    }
    out = {}
    for name, c in sets.items():
        grp = a.groupby(c, dropna=False)[GT_USER]
        entry = {"rows_unique": float((grp.transform("size") == 1).mean())}
        if name in ("QI", "QI+TAC"):
            entry["rows_with_single_distinct_user"] = float((grp.transform("nunique") == 1).mean())
        out[name] = entry
    return out


def check_home_work(a, home_true, work_true):
    n_cells = max(a.loc[~a.enb_supp, "enb_s"].nunique(), 1)
    users_per_tac = a.groupby("tac")[GT_USER].agg(lambda s: frozenset(s.unique()))
    users_per_tac = users_per_tac[users_per_tac.index != "NA"]

    def guess(sub):
        return sub.groupby("tac")["enb_s"].agg(lambda s: s.mode().iat[0])

    ok = a[~a.enb_supp]
    guesses = {
        "home": (guess(ok[ok.hod.between(0, 5)]), home_true),
        "work": (guess(ok[(ok.dow < 5) & ok.hod.between(9, 16)]), work_true),
    }
    out = {}
    for label, (g, truth) in guesses.items():
        res = {}
        for lo, hi in ((1, 1), (2, 3), (4, 10)):
            hits, base, n, users_ok = [], [], 0, set()
            for tac, guess_cell in g.items():
                us = users_per_tac.get(tac)
                if us is None or not (lo <= len(us) <= hi):
                    continue
                true_cells = {truth[u] for u in us if u in truth.index}
                if not true_cells:
                    continue
                n += 1
                hit = guess_cell in true_cells
                hits.append(hit)
                base.append(len(true_cells) / n_cells)
                if hit and len(us) == 1:
                    users_ok |= set(us)
            res[f"tacs_with_{lo}-{hi}_users"] = {
                "tacs_evaluated": n,
                "accuracy": float(np.mean(hits)) if hits else None,
                "random_baseline": float(np.mean(base)) if base else None,
                "single_user_tacs_correctly_located": len(users_ok),
            }
        out[label] = res
    out["share_of_all_users_home_located_via_unique_tac"] = float(
        out["home"]["tacs_with_1-1_users"]["single_user_tacs_correctly_located"]
        / max(a[GT_USER].nunique(), 1)
    )
    return out


def check_linking(a, max_tac_users=50, max_pairs=4_000_000):
    """Link each row at hour h to a row at hour h+1 of the same TAC+province by KPI similarity."""
    feats = cols(a, LINK)
    if not feats:
        return {"skipped": "no KPI columns"}
    X = np.log1p(a[feats].apply(pd.to_numeric, errors="coerce").clip(lower=0))
    X = X.fillna(X.median())
    X = ((X - X.mean()) / X.std().replace(0, 1)).fillna(0).to_numpy(np.float32)
    ucode = pd.factorize(a[GT_USER])[0]
    tu = a.groupby("tac")[GT_USER].transform("nunique")
    cand = a[(tu <= max_tac_users) & (a["tac"] != "NA")]
    blocks = {k: np.asarray(v) for k, v in cand.groupby(["tac", "province", "time_start"]).groups.items()}
    correct = total = skipped = 0
    chance = 0.0
    for (tac, prov, h), i in blocks.items():
        j = blocks.get((tac, prov, h + 1))
        if j is None:
            continue
        if len(i) * len(j) > max_pairs:
            skipped += 1
            continue
        xi, xj = X[i], X[j]
        d = (xi ** 2).sum(1)[:, None] + (xj ** 2).sum(1)[None, :] - 2 * xi @ xj.T
        pred = ucode[j][d.argmin(1)]
        true = ucode[i]
        has = np.isin(true, ucode[j])
        if not has.any():
            continue
        correct += int((pred == true)[has].sum())
        total += int(has.sum())
        vals, cnt = np.unique(ucode[j], return_counts=True)
        cmap = dict(zip(vals.tolist(), cnt.tolist()))
        chance += sum(cmap[u] for u in true[has].tolist()) / len(j)
    if not total:
        return {"rows_evaluated": 0, "skipped_blocks": skipped}
    return {
        "rows_evaluated": total,
        "link_accuracy": correct / total,
        "chance_baseline": chance / total,
        "lift_over_chance": (correct / total) / max(chance / total, 1e-12),
        "skipped_oversized_blocks": skipped,
    }


def check_unsuppress(a, r, sample=5000, seed=0):
    if GT_ROW not in a.columns or GT_ROW not in r.columns:
        return {"skipped": f"needs {GT_ROW} in both files"}
    truth = r.set_index(GT_ROW)
    out = {}

    # (a) application_category == RESTRICTED recovered from KPI/volume columns
    res = a[a.app_supp]
    out["restricted_rows"] = int(len(res))
    if len(res):
        true_cat = truth.loc[res[GT_ROW], "application_category"].astype(str).to_numpy()
        out["restricted_majority_baseline"] = float(pd.Series(true_cat).value_counts(normalize=True).iloc[0])
        try:
            from sklearn.ensemble import HistGradientBoostingClassifier
            feats = cols(a, KPI) + ["hod"]
            train = a[~a.app_supp]
            if len(train) == 0:
                raise ValueError("no unsuppressed rows to train on")
            if len(train) > 150_000:
                train = train.sample(150_000, random_state=seed)
            f = lambda d: d[feats].apply(pd.to_numeric, errors="coerce").to_numpy(float)
            clf = HistGradientBoostingClassifier(max_iter=100, random_state=seed)
            clf.fit(f(train), train["application_category"].astype(str))
            out["restricted_app_recovery_accuracy"] = float((clf.predict(f(res)) == true_cat).mean())
        except ImportError:
            out["restricted_app_recovery_accuracy"] = "scikit-learn not installed"
        except ValueError as e:
            out["restricted_app_recovery_accuracy"] = str(e)
    # (b) enb == 0 recovered from same-TAC rows in neighbouring hours
    sup = a[a.enb_supp]
    out["enb_zero_rows"] = int(len(sup))
    if len(sup):
        known = a[~a.enb_supp]
        idx = {k: (v["time_start"].to_numpy(), v["enb_s"].to_numpy())
               for k, v in known.groupby(["tac", "province"])}
        prov_mode = known.groupby("province")["enb_s"].agg(lambda s: s.mode().iat[0])
        sup = sup.sample(min(sample, len(sup)), random_state=seed)
        answered = hit = hit_prov = 0
        for _, row in sup.iterrows():
            t = truth.at[row[GT_ROW], "enb_s"]
            hit_prov += int(prov_mode.get(row["province"]) == t)
            got = idx.get((row["tac"], row["province"]))
            if got is None:
                continue
            m = np.abs(got[0] - row["time_start"]) <= 2
            if not m.any():
                continue
            vals, cnt = np.unique(got[1][m], return_counts=True)
            answered += 1
            hit += int(vals[cnt.argmax()] == t)
        out["enb_recovery"] = {
            "rows_tried": int(len(sup)),
            "coverage": answered / len(sup),
            "accuracy_when_answered": hit / answered if answered else None,
            "province_mode_baseline": hit_prov / len(sup),
        }
    return out


def check_suppression(a, r):
    flagged = a.app_supp | a.enb_supp
    by_hod = flagged.groupby(a.hod).mean()
    by_prov = flagged.groupby(a.province).mean().sort_values(ascending=False)
    out = {
        "share_app_restricted": float(a.app_supp.mean()),
        "share_enb_zero": float(a.enb_supp.mean()),
        "flag_rate_by_hour_of_day_min_max": [float(by_hod.min()), float(by_hod.max())],
        "flag_rate_top_provinces": {str(k): float(v) for k, v in by_prov.head(3).items()},
    }
    if r is not None:
        out["rows_dropped_share"] = float(1 - len(a) / len(r))
        out["users_with_all_rows_dropped"] = int(len(set(r[GT_USER]) - set(a[GT_USER])))
        rc = r.groupby(["hour", "province"]).size().rename("raw")
        ac = a.groupby(["time_start", "province"]).size().rename("anon")
        ac.index.names = ["hour", "province"]
        m = pd.concat([rc, ac], axis=1).fillna(0)
        out["hour_province_cells_with_missing_rows_(L3_differencing)"] = float((m.anon < m.raw).mean())
        if GT_ROW in a.columns and GT_ROW in r.columns:
            dropped = ~r[GT_ROW].isin(a[GT_ROW])
            dh = dropped.groupby(r.hod).mean()
            out["drop_rate_by_hour_of_day_min_max"] = [float(dh.min()), float(dh.max())]
    return out


def check_l_diversity(a):
    qi = cols(a, QI)
    out = {}
    flags = {
        "tethering": "tethering_data_GB_dl_sum", "video": "im_video_GB_sum", "audio": "im_audio_GB_sum",
    }
    for name, col in flags.items():
        if col not in a.columns:
            continue
        tmp = a[qi].copy()
        tmp["_f"] = (pd.to_numeric(a[col], errors="coerce") > 0).astype(float)
        g = tmp.groupby(qi, dropna=False)["_f"]
        mean, size = g.transform("mean"), g.transform("size")
        out[name] = {
            "overall_rate": float(tmp["_f"].mean()),
            "rows_in_classes_where_all_positive": float(((mean == 1) & (size >= 2)).mean()),
            "rows_in_classes_where_all_negative": float(((mean == 0) & (size >= 2)).mean()),
        }
    return out


def check_points(a, r, ns=(1, 2, 3, 4), trials=300, seed=0):
    """de Montjoye-style test: attacker knows n (hour, cell) points of a target (+ optionally its TAC).
    Candidate users are computed by intersecting user sets per point, which assumes PERFECT row linking
    (oracle). It is therefore a worst case for the defender."""
    rng = np.random.default_rng(seed)
    ok = a[~a.enb_supp]
    idx_plain = ok.groupby(["time_start", "enb_s"])[GT_USER].agg(frozenset).to_dict()
    idx_tac = ok.groupby(["tac", "time_start", "enb_s"])[GT_USER].agg(frozenset).to_dict()
    pts = r[[GT_USER, "hour", "enb_s", "tac"]].drop_duplicates()
    users = pts[GT_USER].unique()
    pts_by_user = {u: g[["hour", "enb_s", "tac"]].to_numpy() for u, g in pts.groupby(GT_USER)}
    out = {}
    for n in ns:
        elig = [u for u in users if len(pts_by_user[u]) >= n]
        if not elig:
            continue
        pick = rng.choice(len(elig), size=min(trials, len(elig)), replace=False)
        stats = {"points_only": [], "points_plus_tac": []}
        for p in pick:
            u = elig[p]
            P = pts_by_user[u]
            sel = P[rng.choice(len(P), n, replace=False)]
            sets_p = [idx_plain.get((h, e), frozenset()) for h, e, t in sel]
            sets_t = [idx_tac.get((t, h, e), frozenset()) for h, e, t in sel]
            for key, sets in (("points_only", sets_p), ("points_plus_tac", sets_t)):
                cand = sets[0].intersection(*sets[1:])
                stats[key].append((u in cand, len(cand)))
        out[f"n_points={n}"] = {
            key: {
                "target_in_candidates": float(np.mean([s[0] for s in v])),
                "uniquely_identified": float(np.mean([s[0] and s[1] == 1 for s in v])),
                "median_candidate_set": float(np.median([s[1] for s in v])),
            }
            for key, v in stats.items()
        }
    return out


# --------------------------------------------------------------------------- selftest data
def make_synthetic(n_users=1200, days=4, seed=1):
    rng = np.random.default_rng(seed)
    provinces = [f"P{i}" for i in range(5)]
    cells = {p: np.arange(i * 8 + 1, i * 8 + 9) for i, p in enumerate(provinces)}
    n_tac = 60
    tacs = [f"{35000000 + i * 7919:08d}" for i in range(n_tac)]
    w = 1 / np.arange(1, n_tac + 1) ** 1.2
    w /= w.sum()
    apps = ["video", "audio", "browsing", "social", "tethering"]
    base = 1_750_000_000 // 86400 * 86400
    rows = []
    for u in range(n_users):
        prov = provinces[rng.integers(5)]
        home, work = rng.choice(cells[prov], 2, replace=False)
        tac = tacs[rng.choice(n_tac, p=w)]
        imei = tac + f"{rng.integers(0, 10**7):07d}"
        msisdn, imsi = f"3585{rng.integers(10**7, 10**8)}", f"24491{rng.integers(10**9, 10**10)}"
        prof = rng.lognormal(0, 0.5, 10)
        rat = rng.choice(["4G", "5G"], p=[0.7, 0.3])
        for d in range(days):
            for hr in range(24):
                if rng.random() > 0.5:
                    continue
                ts = base + (d * 24 + hr) * 3600 + int(rng.integers(0, 3600))
                dow = (ts // 86400 + 3) % 7
                if hr < 7 or hr >= 19:
                    enb = home
                elif dow < 5 and rng.random() < 0.8:
                    enb = work
                else:
                    enb = rng.choice(cells[prov])
                for app in rng.choice(apps, size=int(rng.integers(1, 3)), replace=False):
                    k = prof * rng.lognormal(0, 0.12, 10)
                    vol = rng.lognormal(-2, 1)
                    rows.append({
                        "time_start": ts, "msisdn": msisdn, "imsi": imsi, "imei": imei,
                        "tp_dl_avg": k[0] * 20000, "tp_ul_avg": k[1] * 5000,
                        "tp_dl_filtered_avg": k[2] * 30000, "cont_rtt_radio_avg": k[3] * 40,
                        "cont_rtt_internet_avg": k[4] * 60, "initial_rtt_radio_avg": k[5] * 30,
                        "tcp_retrans_byte_ratio_downlink_avg": k[6] * 0.01,
                        "tcp_retrans_byte_ratio_uplink_avg": k[7] * 0.01,
                        "http_response_time_avg": k[8] * 200, "http_sr_avg": min(k[9] * 0.9, 1.0),
                        "data_GB_sum": vol,
                        "im_video_GB_sum": vol * 0.8 if app == "video" else 0.0,
                        "im_audio_GB_sum": vol * 0.5 if app == "audio" else 0.0,
                        "tethering_data_GB_dl_sum": vol * 0.6 if app == "tethering" else 0.0,
                        "radio_access_type": rat, "province": prov,
                        "application_category": app, "enb": int(enb),
                    })
    df = pd.DataFrame(rows)
    df[GT_USER] = df["msisdn"]
    df[GT_ROW] = np.arange(len(df))
    return df


def reference_anonymise(raw, k=10, sig=3):
    """Re-implementation of the documented pipeline (for the self-test only)."""
    d = raw.drop(columns=["msisdn", "imsi"], errors="ignore").copy()
    d["imei"] = d["imei"].astype("string").str[:8]
    d["time_start"] = (d["time_start"] // 3600).astype("int64")
    for c in cols(d, KPI):
        d[c] = sig_round(d[c].to_numpy(float), sig)
    qi = cols(d, QI)
    small = lambda: d.groupby(qi)[GT_USER].transform("size") < k
    d.loc[small(), "application_category"] = "RESTRICTED"
    d.loc[small(), "enb_id"] = 0
    return d[~small()].reset_index(drop=True)


# --------------------------------------------------------------------------- main
def _native(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


def headline(res):
    lines = ["", "=== HEADLINE ==="]
    g = lambda *ks: _dig(res, ks)
    lines.append(f"rows in classes with < k distinct users : {g('k_anonymity', 'share_rows_in_classes_with_users_lt_k')}")
    lines.append(f"rows whose TAC is shared by <= 3 users  : {g('tac_pseudo_id', 'rows_with_tac_shared_by_le_3_users')}")
    lines.append(f"home located via single-user TAC (share): {g('home_work', 'share_of_all_users_home_located_via_unique_tac')}")
    lines.append(f"row-linking accuracy / chance          : {g('linking', 'link_accuracy')} / {g('linking', 'chance_baseline')}")
    lines.append(f"row order leaks user                   : {g('format', 'row_order', 'LEAK')}")
    lines.append(f"app-category recovery accuracy         : {g('unsuppress', 'restricted_app_recovery_accuracy')}")
    lines.append(f"unique on QI+TAC+KPI tuple             : {g('uniqueness', 'QI+TAC+KPI_tuple', 'rows_unique')}")
    return "\n".join(lines)


def _dig(d, ks):
    for k in ks:
        if not isinstance(d, dict) or k not in d:
            return "n/a"
        d = d[k]
    return round(d, 4) if isinstance(d, float) else d


def run(raw, anon, args):
    a = prep_anon(anon, args.tz_offset)
    r = prep_raw(raw, args.tz_offset) if raw is not None else None
    if GT_USER not in a.columns:
        sys.exit(f"{GT_USER} missing from anon file - see the workflow in the docstring")
    res = {
        "format": check_format(a, args.sig_figs),
        "k_anonymity": check_k(a, args.k_min),
        "tac_pseudo_id": check_tac(a),
        "uniqueness": check_uniqueness(a),
        "l_diversity": check_l_diversity(a),
        "linking": check_linking(a),
    }
    if r is not None:
        home, work = true_anchors(r)
        res["home_work"] = check_home_work(a, home, work)
        res["unsuppress"] = check_unsuppress(a, r, seed=args.seed)
        res["suppression"] = check_suppression(a, r)
        res["point_uniqueness"] = check_points(a, r, trials=args.trials, seed=args.seed)
    else:
        res["suppression"] = check_suppression(a, None)
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", help="raw CSV with gt_user and gt_row_id")
    ap.add_argument("--anon", help="anonymised CSV with gt_user and gt_row_id passed through")
    ap.add_argument("--add-gt", nargs=2, metavar=("IN", "OUT"))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--k-min", type=int, default=10)
    ap.add_argument("--sig-figs", type=int, default=3)
    ap.add_argument("--tz-offset", type=int, default=0, help="hours to add to UTC for local time (night/work windows)")
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--report", default="attack_report.json")
    args = ap.parse_args()

    if args.add_gt:
        add_gt(*args.add_gt)
        return
    if args.selftest:
        raw = make_synthetic()
        anon = reference_anonymise(raw, args.k_min, args.sig_figs)
        print(f"selftest: raw {len(raw)} rows -> anon {len(anon)} rows")
    else:
        if not args.anon:
            ap.error("--anon is required (or use --selftest / --add-gt)")
        raw = read(args.raw) if args.raw else None
        anon = read(args.anon)

    res = run(raw, anon, args)
    with open(args.report, "w") as fh:
        json.dump(res, fh, indent=2, default=_native)
    print(json.dumps(res, indent=2, default=_native))
    print(headline(res))
    print(f"\nfull report: {args.report}")


if __name__ == "__main__":
    main()
