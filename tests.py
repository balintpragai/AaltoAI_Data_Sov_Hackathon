#!/usr/bin/env python3
"""
privacy_tests.py - anonymisation test suite for the mobile-network dataset.

Runs the automatable tests on a CSV and prints one PASS/WARN/FAIL line per test.
A full JSON report (with the dataset SHA-256, parameters and thresholds) is
written for the audit trail (GDPR Art. 5(2) accountability).

USAGE
  python privacy_tests.py release.csv                       # test a release candidate
  python privacy_tests.py release.csv --holdout ctrl.csv    # add a real control set
  python privacy_tests.py --demo raw                        # synthetic, badly anonymised
  python privacy_tests.py --demo hardened                   # synthetic, better anonymised

Run it on the RELEASE CANDIDATE (the data after anonymisation), not the raw data.

IMPORTANT
  * Thresholds are engineering heuristics (my recommendations), NOT legal values.
    No regulation prescribes a value of k, epsilon, or a re-link rate.
  * A PASS here means "these specific attacks did not succeed". It is not proof
    of anonymity under GDPR Recital 26. See the list of excluded tests.
Requires: python>=3.9, pandas, numpy.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import platform
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------------------------------------------- schema ---
TIME, ENB, PROV, RAT, APP = ("time_start", "enb", "province",
                             "radio_access_type", "application_category")
ID_COLS = ["msisdn", "imsi", "imei"]
VOLUME_COLS = ["data_GB_sum", "im_video_GB_sum", "im_audio_GB_sum",
               "tethering_data_GB_dl_sum"]
METRIC_COLS = ["tp_dl_avg", "tp_ul_avg", "tp_dl_filtered_avg",
               "cont_rtt_radio_avg", "cont_rtt_internet_avg",
               "initial_rtt_radio_avg", "tcp_retrans_byte_ratio_downlink_avg",
               "tcp_retrans_byte_ratio_uplink_avg", "http_response_time_avg",
               "http_sr_avg"]
# Category-name keywords that hint at GDPR Art. 9 special-category inference.
SENSITIVE_HINTS = r"vpn|\btor\b|dating|health|medic|pharma|relig|church|mosque|gambl|casino|adult|porn|lgbt|politic"
HASHES = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}
RANK = {"PASS": 0, "INFO": 0, "SKIP": 0, "WARN": 1, "FAIL": 2, "ERROR": 2}


def res(name, status, summary, **details):
    return {"test": name, "status": status, "summary": summary, "details": details}


def worst(statuses):
    return max(statuses, key=lambda s: RANK[s]) if statuses else "SKIP"


# ---------------------------------------------------------------- helpers ---
def luhn_ok(s: str) -> bool:
    """Luhn check (used for IMEI). Parenthesised on purpose - see note in text."""
    if not s.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(s)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def prep(df: pd.DataFrame, tz: str) -> pd.DataFrame:
    """Derive helper columns (prefixed '_'): hour bucket, hour-of-day."""
    if TIME in df.columns:
        t = df[TIME]
        if pd.api.types.is_numeric_dtype(t):
            unit = "ms" if t.dropna().abs().median() > 1e11 else "s"
            ts = pd.to_datetime(t, unit=unit, errors="coerce", utc=True)
        else:
            ts = pd.to_datetime(t, errors="coerce", utc=True)
        ts = ts.dt.tz_convert(tz)
        df["_ts"] = ts
        df["_hour"] = ts.dt.floor("60min")
        df["_hod"] = ts.dt.hour
    return df


def base_qi(df):
    return [c for c in ["_hour", ENB, PROV, RAT] if c in df.columns]


def k_per_row(df, qi, uc):
    """k per record = number of DISTINCT subscribers in its equivalence class
    (rows, if the file has no subscriber column)."""
    codes = df.groupby(qi, dropna=False, observed=True, sort=False).ngroup()
    if uc:
        tmp = pd.DataFrame({"c": codes.values, "u": df[uc].values})
        size = tmp.groupby("c")["u"].nunique()
    else:
        size = codes.value_counts()
    return codes.map(size)


def modal_cell(d, uc):
    c = d.groupby([uc, ENB]).size().reset_index(name="n")
    c = c.sort_values("n", ascending=False).drop_duplicates(uc)
    return c.set_index(uc)[ENB]


# ------------------------------------------------------------------ tests ---
def t1_identifier_scan(df, a):
    """WP29 'identification': are raw identifiers still present anywhere?"""
    name = "T1 identifier scan"
    cols = [c for c in df.columns if not c.startswith("_") and c != TIME
            and not pd.api.types.is_float_dtype(df[c])]
    sample = df.sample(min(len(df), a.scan_rows), random_state=a.seed)
    findings = []
    for c in cols:
        s = sample[c].dropna().astype(str).str.strip()
        if s.empty:
            continue
        is15 = s.str.fullmatch(r"\d{15}")
        rates = {
            "IMEI-like (15 digits + Luhn)": sum(luhn_ok(v) for v in s[is15]) / len(s),
            "E.164 phone-like": float(s.str.fullmatch(r"\+?[1-9]\d{7,14}").mean()),
        }
        imsi_mask = is15 & (s.str.startswith(a.mcc_mnc) if a.mcc_mnc else True)
        rates["IMSI-like (15 digits" + (f", prefix {a.mcc_mnc})" if a.mcc_mnc else ")")] = float(imsi_mask.mean())
        for pat, r in rates.items():
            if r > 0:
                findings.append({"column": c, "pattern": pat, "share": round(r, 4)})
    if not findings:
        return res(name, "PASS", "No column contains IMEI/IMSI/MSISDN-shaped values.")
    st = "FAIL" if any(f["share"] >= 0.01 for f in findings) else "WARN"
    cols_hit = sorted({f["column"] for f in findings})
    return res(name, st, f"Identifier-shaped values in column(s): {', '.join(cols_hit)}. "
                         "(15-digit IDs can hit several patterns; int columns such as cell IDs can be false positives.)",
               findings=findings)


def crack(targets, algo, prefix, ndigits, max_cand):
    h = getattr(hashlib, algo)
    variants = [prefix, prefix.lstrip("+") if prefix.startswith("+") else "+" + prefix]
    space = 10 ** ndigits
    limit = min(space, max_cand)
    found, tried, t0 = {}, 0, time.time()
    for i in range(limit):
        tail = f"{i:0{ndigits}d}"
        for v in variants:
            cand = v + tail
            tried += 1
            d = h(cand.encode()).hexdigest()
            if d in targets:
                found[d] = cand
        if len(found) == len(targets):
            break
    dt = max(time.time() - t0, 1e-9)
    return found, tried, tried / dt, limit / space, space * len(variants)


def t2_pseudonym(df, a):
    """Pseudonym strength: can the subscriber token be reversed by enumeration?"""
    name = "T2 pseudonym brute-force"
    uc = a.user_col
    if not uc:
        return res(name, "SKIP", "No subscriber column in file: nothing to attack.")
    vals = df[uc].dropna().astype(str).str.strip().unique()
    if len(vals) == 0:
        return res(name, "SKIP", "Subscriber column empty.")
    probe = vals[:5000]
    digit_share = float(np.mean([bool(re.fullmatch(r"\+?\d{8,15}", v)) for v in probe]))
    if digit_share > 0.9:
        return res(name, "FAIL", f"{digit_share:.0%} of '{uc}' values look like raw phone/IMSI numbers.")
    hexlike = float(np.mean([bool(re.fullmatch(r"[0-9a-fA-F]+", v)) and len(v) in HASHES for v in probe]))
    if hexlike <= 0.9:
        return res(name, "PASS", "Tokens are neither digit-strings nor digests (opaque tokens). "
                                 "Key/mapping-table separation must be checked manually (excluded test).")
    algo = HASHES[len(probe[0])]
    if not a.msisdn_prefix:
        return res(name, "WARN", f"Values look like {algo} digests. A keyed HMAC and an unkeyed hash look "
                                 "identical; pass --msisdn-prefix/--msisdn-digits to attempt enumeration.")
    rng = np.random.default_rng(a.seed)
    tg = rng.choice(vals, size=min(a.n_targets, len(vals)), replace=False)
    targets = {t.lower() for t in tg}
    found, tried, rate, coverage, full = crack(targets, algo, a.msisdn_prefix, a.msisdn_digits, a.max_candidates)
    eta_h = full / rate / 3600
    det = dict(algorithm=algo, targets=len(targets), recovered=len(found), candidates_tried=tried,
               python_hashes_per_sec=round(rate), space_covered=round(coverage, 4),
               est_hours_full_space_in_python=round(eta_h, 2),
               example_recovered=list(found.values())[:3])
    if found:
        return res(name, "FAIL", f"Recovered {len(found)}/{len(targets)} numbers from {algo} digests "
                                 f"({rate:,.0f} H/s in Python; a GPU is ~10^4-10^5x faster). Use a keyed HMAC.", **det)
    if coverage >= 1:
        return res(name, "PASS", "Entire numbering space enumerated, nothing recovered (consistent with a keyed "
                                 "HMAC or a different input format). Key separation still needs a manual check.", **det)
    return res(name, "WARN", f"Nothing recovered but only {coverage:.1%} of the space was tried; raise --max-candidates.", **det)


def t3_kanon(df, a):
    """WP29 'singling out': k-anonymity and risk models over several QI sets."""
    name = "T3 k-anonymity / risk"
    base = base_qi(df)
    if not base:
        return res(name, "SKIP", "No quasi-identifier columns found.")
    sets = {}
    if "_hour" in base and ENB in base:
        sets["hour+cell"] = ["_hour", ENB]
    sets["all base QIs"] = base
    if APP in df:
        sets["base QIs + application"] = base + [APP]
    vol = [c for c in VOLUME_COLS if c in df]
    if vol:
        sets["base QIs + app + volumes (as released)"] = base + ([APP] if APP in df else []) + vol
    out, sts = {}, []
    for label, qi in sets.items():
        k = k_per_row(df, qi, a.user_col)
        p1, plt = float((k == 1).mean()), float((k < a.k_min).mean())
        st = "FAIL" if p1 > 0 else ("WARN" if plt > a.max_share_below_k else "PASS")
        sts.append(st)
        out[label] = dict(status=st, qi=qi, min_k=int(k.min()), median_k=float(k.median()),
                          share_k1=round(p1, 4), share_below_k=round(plt, 4),
                          prosecutor_max_risk=round(1 / k.min(), 4),
                          marketer_avg_risk=round(float((1 / k).mean()), 4))
    unit = "distinct subscribers" if a.user_col else "rows (no subscriber column)"
    return res(name, worst(sts), f"k counted in {unit}; k_min={a.k_min}. Journalist risk needs a population table "
                                 "and equals prosecutor risk if the file is the whole population.", by_qi_set=out)


def t4_unicity(df, a):
    """de Montjoye-style unicity: how many known (hour, cell) points single out a user?"""
    name = "T4 trajectory unicity"
    uc = a.user_col
    if not uc or "_hour" not in df or ENB not in df:
        return res(name, "SKIP", "Needs subscriber, time and cell columns.")
    pts = df[[uc, "_hour", ENB]].dropna().drop_duplicates()
    pts["pid"] = pts.groupby(["_hour", ENB], sort=False).ngroup()
    pts["uid"] = pd.factorize(pts[uc])[0]

    def split_by(key, val):
        p = pts.sort_values(key)
        keys, starts = np.unique(p[key].values, return_index=True)
        return dict(zip(keys, np.split(p[val].values, starts[1:])))

    point_users, user_points = split_by("pid", "uid"), split_by("uid", "pid")
    rng = np.random.default_rng(a.seed)
    out = {}
    for p in (2, 3, 4):
        eligible = [u for u, v in user_points.items() if len(v) >= p]
        if len(eligible) < 20:
            out[p] = None
            continue
        hits = 0
        n = min(a.n_samples, len(eligible))
        for u in rng.choice(eligible, n, replace=False):
            chosen = rng.choice(user_points[u], p, replace=False)
            cand = point_users[chosen[0]]
            for c in chosen[1:]:
                cand = np.intersect1d(cand, point_users[c], assume_unique=True)
            hits += len(cand) == 1
        out[p] = round(hits / n, 4)
    u4 = out.get(4)
    if u4 is None:
        return res(name, "SKIP", "Too few users with >=4 distinct (hour, cell) points.", unicity=out)
    st = "FAIL" if u4 > a.unicity_fail else ("WARN" if u4 > a.unicity_warn else "PASS")
    return res(name, st, f"Share of users uniquely identified by p random known points: "
                         f"p=2:{out[2]}, p=3:{out[3]}, p=4:{out[4]} (fail>{a.unicity_fail}, warn>{a.unicity_warn}).",
               unicity=out)


def t5_home_cell(df, a):
    """Inference: do night-time modal cells (proxy for home) expose few-person areas?"""
    name = "T5 home-cell exposure"
    uc = a.user_col
    if not uc or "_hod" not in df or ENB not in df:
        return res(name, "SKIP", "Needs subscriber, time and cell columns.")
    night = df[(df["_hod"] >= 22) | (df["_hod"] < 6)]
    if night.empty or df["_hod"].nunique() <= 1:
        return res(name, "SKIP", "No night-time resolution left in time_start (good for privacy).")
    home = modal_cell(night, uc)
    per_cell = home.value_counts()
    share = float((home.map(per_cell) < a.k_min).mean())
    st = "FAIL" if share > 0.05 else ("WARN" if share > 0.01 else "PASS")
    return res(name, st, f"{share:.1%} of users have a night-time home cell shared by <{a.k_min} users "
                         f"({len(home)} users assessed). Time zone used: {a.tz}.", users=len(home))


def t6_relink(df, a):
    """WP29 'linkability': can metric fingerprints re-link a user across two time halves?"""
    name = "T6 metric fingerprint re-link"
    uc = a.user_col
    metrics = [c for c in METRIC_COLS + VOLUME_COLS if c in df and pd.api.types.is_numeric_dtype(df[c])]
    if not uc or "_ts" not in df or not metrics:
        return res(name, "SKIP", "Needs subscriber, time and numeric metric columns.")
    ts = df["_ts"].dropna().sort_values()
    cut = ts.iloc[len(ts) // 2]
    A, B = df[df["_ts"] < cut], df[df["_ts"] >= cut]
    gA, gB = A.groupby(uc)[metrics].mean(), B.groupby(uc)[metrics].mean()
    common = gA.index.intersection(gB.index)
    gA, gB = gA.loc[common].dropna(), gB.loc[common].dropna()
    common = gA.index.intersection(gB.index)
    if len(common) < 50:
        return res(name, "SKIP", f"Only {len(common)} subscribers appear in both halves (need >=50). "
                                 "If pseudonyms rotate, test with a file that keeps ground-truth ids.")
    rng = np.random.default_rng(a.seed)
    if len(common) > a.max_users:
        common = pd.Index(rng.choice(common, a.max_users, replace=False))
    gA, gB = gA.loc[common], gB.loc[common]
    prep_ = lambda g: np.log1p(np.clip(g.values.astype(float), 0, None))
    X, Y = prep_(gA), prep_(gB)
    both = np.vstack([X, Y])
    mu, sd = both.mean(0), both.std(0)
    sd[sd == 0] = 1
    X, Y = (X - mu) / sd, (Y - mu) / sd
    n = len(X)
    D = (X ** 2).sum(1)[:, None] + (Y ** 2).sum(1)[None, :] - 2 * X @ Y.T
    diag = np.arange(n)

    def rates(M):
        top1 = float((M.argmin(1) == diag).mean())
        idx5 = np.argpartition(M, min(4, n - 1), axis=1)[:, :5]
        return top1, float((idx5 == diag[:, None]).any(1).mean())

    t1m, t5m = rates(D)
    out = dict(subscribers=n, baseline_top1=round(1 / n, 5), baseline_top5=round(5 / n, 5),
               metrics_used=metrics, metrics_only=dict(top1=round(t1m, 4), top5=round(t5m, 4)))
    worst_rate = t1m
    if ENB in df:
        hA = np.asarray(modal_cell(A, uc).reindex(common).astype(str), dtype=object)
        hB = np.asarray(modal_cell(B, uc).reindex(common).astype(str), dtype=object)
        t1h, t5h = rates(D + 1e3 * (hA[:, None] != hB[None, :]))
        out["metrics_plus_home_cell"] = dict(top1=round(t1h, 4), top5=round(t5h, 4))
        worst_rate = max(worst_rate, t1h)
    lift = worst_rate * n
    st = "FAIL" if (worst_rate > 0.05 and lift > 10) else ("WARN" if (worst_rate > 0.01 and lift > 3) else "PASS")
    return res(name, st, f"Best re-link top-1 rate {worst_rate:.1%} vs random {1 / n:.2%} (x{lift:.0f} lift), N={n}. "
                         "Fail: >5% and >10x; warn: >1% and >3x.", **out)


def decimals_needed(x):
    x = x[np.isfinite(x)]
    for d in range(0, 10):
        if np.allclose(np.round(x, d), x, rtol=0, atol=1e-9):
            return d
    return 10


def t7_precision(df, a):
    """Fingerprinting by exact values: precision, uniqueness, outliers/top-coding."""
    name = "T7 value precision & outliers"
    cols = [c for c in VOLUME_COLS + METRIC_COLS if c in df and pd.api.types.is_numeric_dtype(df[c])]
    if not cols:
        return res(name, "SKIP", "No numeric measurement columns.")
    per, sts = {}, []
    for c in cols:
        s = df[c].dropna()
        if s.empty:
            continue
        vc = s.value_counts()
        uniq = float((vc == 1).sum() / len(s))
        dec = decimals_needed(s.sample(min(len(s), 20000), random_state=a.seed).values.astype(float))
        p50, p999, mx = float(s.quantile(.5)), float(s.quantile(.999)), float(s.max())
        outlier = p999 > 0 and mx / p999 > 10
        vol = c in VOLUME_COLS
        if vol and uniq > 0.5:
            st = "FAIL"
        elif (vol and (dec > 3 or uniq > 0.2 or outlier)) or (not vol and dec > 3 and uniq > 0.5):
            st = "WARN"
        else:
            st = "PASS"
        sts.append(st)
        per[c] = dict(status=st, unique_value_share=round(uniq, 4), decimals=dec,
                      p50=p50, p99_9=p999, max=mx, max_over_p99_9_gt_10=bool(outlier))
    return res(name, worst(sts), "Exact, rarely repeated values act as keys; round/bin them and top-code outliers.",
               per_column=per)


def t8_ldiv(df, a):
    """WP29 'inference': l-diversity / t-closeness of application_category + Art.9 hints."""
    name = "T8 l-diversity / t-closeness"
    if APP not in df:
        return res(name, "SKIP", f"No {APP} column.")
    qi = base_qi(df)
    if not qi:
        return res(name, "SKIP", "No quasi-identifier columns.")
    cls = df.groupby(qi, dropna=False, observed=True, sort=False).ngroup()
    ct = pd.crosstab(cls, df[APP].astype(str))
    n_c = ct.sum(axis=1)
    glob = ct.sum(axis=0) / ct.values.sum()
    dist = ct.div(n_c, axis=0)
    tvd = 0.5 * (dist - glob).abs().sum(axis=1)
    top = dist.max(axis=1)
    big = n_c >= a.k_min
    w = n_c[big]
    homog = float(w[(top[big] > 0.9)].sum() / max(w.sum(), 1))
    far = float(w[(tvd[big] > a.t_close)].sum() / max(w.sum(), 1))
    flagged = [c for c in glob.index if re.search(SENSITIVE_HINTS, c, re.I)]
    rare = glob[glob < 0.01]
    st = "FAIL" if homog > 0.20 else ("WARN" if (homog > 0.05 or far > 0.20 or flagged) else "PASS")
    return res(name, st,
               f"Among classes with >={a.k_min} records: {homog:.1%} of records sit in classes where one category "
               f"exceeds 90%; {far:.1%} in classes with TVD>{a.t_close} from the global mix. "
               f"Art.9-hint categories: {flagged or 'none'}.",
               homogeneous_record_share=round(homog, 4), t_closeness_violation_share=round(far, 4),
               art9_hint_categories=flagged, rare_categories_lt_1pct=rare.round(4).to_dict())


def t9_inference(df, a, holdout):
    """Inference attack vs. control (Anonymeter-style, simplified).
    Attacker knows a target's QIs, looks up the class majority of application_category in the
    release. Success on members vs. non-members: the gap is privacy leakage; the control
    accuracy is what inference from population statistics alone achieves."""
    name = "T9 inference attack vs control"
    aux = base_qi(df)
    if APP not in df or not aux:
        return res(name, "SKIP", "Needs application_category and quasi-identifiers.")
    rng = np.random.default_rng(a.seed)
    w = df[aux + [APP]].astype(str)
    uc = a.user_col
    if holdout is not None:
        rel, ctl, mode = w, holdout[aux + [APP]].astype(str), "external holdout file"
    elif uc:
        users = np.array(df[uc].dropna().unique(), dtype=object)
        rng.shuffle(users)
        mem = set(users[: int(0.7 * len(users))])
        mask = df[uc].isin(mem).values
        rel, ctl, mode = w[mask], w[~mask], "internal 70/30 subscriber split (weaker than a real holdout)"
    else:
        mask = rng.random(len(w)) < 0.7
        rel, ctl, mode = w[mask], w[~mask], "internal 70/30 row split (no subscriber column)"
    if len(rel) < 100 or len(ctl) < 100:
        return res(name, "SKIP", "Too few rows for a member/control comparison.")
    cnt = rel.groupby(aux + [APP]).size().rename("n").reset_index()
    best = cnt.sort_values("n", ascending=False).drop_duplicates(aux)[aux + [APP]].rename(columns={APP: "guess"})
    glob = rel[APP].mode().iloc[0]

    def acc(t):
        t = t.sample(min(len(t), a.n_attacks), random_state=a.seed)
        m = t.merge(best, on=aux, how="left")
        g = m["guess"].fillna(glob).values
        p = float((g == m[APP].values).mean())
        return p, len(t), 1.96 * (p * (1 - p) / len(t)) ** 0.5

    pm, nm, cim = acc(rel)
    pc, nc, cic = acc(ctl)
    risk = max(0.0, (pm - pc) / (1 - pc)) if pc < 1 else 0.0
    st = "FAIL" if risk >= 0.2 else ("WARN" if risk >= 0.05 else "PASS")
    return res(name, st, f"Attack accuracy on members {pm:.1%} (+/-{cim:.1%}) vs control {pc:.1%} (+/-{cic:.1%}); "
                         f"inference risk={risk:.2f} (warn>=0.05, fail>=0.2). Control set: {mode}.",
               members_accuracy=round(pm, 4), control_accuracy=round(pc, 4), risk=round(risk, 4),
               attacks_members=nm, attacks_control=nc, aux_columns=aux)


# ------------------------------------------------------------ demo data -----
def _imei():
    body = [int(x) for x in np.random.randint(0, 10, 14)]
    for chk in range(10):
        if luhn_ok("".join(map(str, body)) + str(chk)):
            return "".join(map(str, body)) + str(chk)


def make_demo(kind: str, n_users=800, days=14, seed=1) -> pd.DataFrame:
    """Synthetic data. 'raw' = weak pseudonyms, raw IMEI, fine time/cell. 'hardened' = mitigated."""
    np.random.seed(seed)
    rng = np.random.default_rng(seed)
    nums = rng.choice(10 ** 5, n_users, replace=False)
    msisdn = ["+358401" + f"{n:05d}" for n in nums]
    cells = np.arange(1, 61)
    cw = 1 / np.arange(1, 61)
    cw /= cw.sum()
    apps = np.array(["web", "video", "social", "music", "gaming", "vpn", "dating_app", "health_portal"])
    ap = np.array([.30, .25, .20, .10, .10, .02, .02, .01])
    base = rng.lognormal(mean=[3, 2, 3, 3, 2, 0, 1.5, 0], sigma=0.6, size=(n_users, 8))
    home = rng.choice(cells, n_users, p=cw)
    work = rng.choice(cells, n_users)
    rows = []
    t0 = pd.Timestamp("2026-03-02", tz="UTC")
    for u in range(n_users):
        for d in range(days):
            for _ in range(rng.integers(5, 10)):
                h = int(rng.integers(0, 24))
                cell = home[u] if (h >= 22 or h < 7) else rng.choice([home[u], work[u], rng.choice(cells)])
                m = base[u] * rng.lognormal(0, 0.08, 8)
                rows.append((t0 + pd.Timedelta(days=d, hours=h, minutes=int(rng.integers(0, 60))), u, cell, m,
                             rng.choice(apps, p=ap)))
    df = pd.DataFrame({
        "time_start": [r[0].isoformat() for r in rows],
        "u": [r[1] for r in rows],
        "enb": [r[2] for r in rows],
        "application_category": [r[4] for r in rows],
    })
    M = np.vstack([r[3] for r in rows])
    df["tp_dl_avg"], df["tp_ul_avg"], df["cont_rtt_radio_avg"] = M[:, 0], M[:, 1], M[:, 2]
    df["cont_rtt_internet_avg"], df["http_response_time_avg"] = M[:, 3], M[:, 4]
    df["data_GB_sum"], df["im_video_GB_sum"], df["tethering_data_GB_dl_sum"] = M[:, 5], M[:, 6], M[:, 7]
    df["province"] = "P" + (df["enb"] % 5).astype(str)
    df["radio_access_type"] = rng.choice(["4G", "5G-NSA", "3G"], len(df), p=[.7, .2, .1])
    ms = np.array(msisdn)[df["u"].values]
    if kind == "raw":
        df["msisdn"] = [hashlib.sha256(m.encode()).hexdigest() for m in ms]  # unkeyed hash: weak
        df["imei"] = np.array([_imei() for _ in range(n_users)])[df["u"].values]  # raw IMEI leaked
        return df.drop(columns="u")
    key = b"secret-kept-in-a-vault"
    df["msisdn"] = [hmac.new(key, m.encode(), hashlib.sha256).hexdigest() for m in ms]
    ts = pd.to_datetime(df["time_start"], utc=True).dt.floor("6h")
    df["time_start"] = ts.dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    df["enb"] = "C" + (df["enb"] // 12).astype(str)
    df["application_category"] = df["application_category"].replace(
        {"vpn": "other", "dating_app": "other", "health_portal": "other"})
    for c in VOLUME_COLS + METRIC_COLS:
        if c in df:
            df[c] = np.round(df[c], 0)
    return df.drop(columns="u")


# ------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="release-candidate CSV")
    ap.add_argument("--holdout", help="CSV of subscribers NOT in the release, same pipeline (control set)")
    ap.add_argument("--user-col", help="subscriber/pseudonym column (default: msisdn, else imsi)")
    ap.add_argument("--out", default="outputs/privacy_report.json")
    ap.add_argument("--demo", choices=["raw", "hardened"], help="generate and test synthetic data")
    ap.add_argument("--tz", default="UTC", help="time zone for hour-of-day (e.g. Europe/Helsinki)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--k-min", type=int, default=10)
    ap.add_argument("--max-share-below-k", type=float, default=0.01)
    ap.add_argument("--unicity-warn", type=float, default=0.01)
    ap.add_argument("--unicity-fail", type=float, default=0.05)
    ap.add_argument("--t-close", type=float, default=0.3)
    ap.add_argument("--n-samples", type=int, default=500, help="unicity samples per p")
    ap.add_argument("--n-attacks", type=int, default=1000, help="inference attacks per group")
    ap.add_argument("--max-users", type=int, default=2000, help="re-link test subscriber cap")
    ap.add_argument("--scan-rows", type=int, default=100000)
    ap.add_argument("--mcc-mnc", default="", help="e.g. 24491 to tighten IMSI pattern")
    ap.add_argument("--msisdn-prefix", default="", help="e.g. +358 ; enables T2 enumeration")
    ap.add_argument("--msisdn-digits", type=int, default=9, help="digits after the prefix")
    ap.add_argument("--max-candidates", type=int, default=3_000_000)
    ap.add_argument("--n-targets", type=int, default=200)
    a = ap.parse_args()
    Path("outputs").mkdir(parents=True, exist_ok=True)
    if a.demo:
        df = make_demo(a.demo)
    
        a.csv = str(Path("outputs") / f"demo_{a.demo}.csv")
        df.to_csv(a.csv, index=False)
        if a.demo == "raw":
            a.msisdn_prefix, a.msisdn_digits = "+358401", 5
        print(f"[demo] wrote {a.csv} ({len(df):,} rows)")
    if not a.csv:
        ap.error("give a CSV path or --demo")

    hdr = pd.read_csv(a.csv, nrows=0).columns
    df = pd.read_csv(a.csv, dtype={c: str for c in ID_COLS if c in hdr}, low_memory=False)
    a.user_col = a.user_col or next((c for c in ID_COLS[:2] if c in df.columns), None)
    df = prep(df, a.tz)
    holdout = prep(pd.read_csv(a.holdout, dtype={c: str for c in ID_COLS if c in hdr}, low_memory=False), a.tz) \
        if a.holdout else None

    tests = [t1_identifier_scan, t2_pseudonym, t3_kanon, t4_unicity, t5_home_cell,
             t6_relink, t7_precision, t8_ldiv, lambda d, x: t9_inference(d, x, holdout)]
    results = []
    print(f"\nFile: {a.csv} | rows: {len(df):,} | subscriber column: {a.user_col}\n" + "-" * 78)
    for t in tests:
        try:
            r = t(df, a)
        except Exception as e:  # keep going; report the failure of the test itself
            r = res(getattr(t, "__name__", "test"), "ERROR", f"{type(e).__name__}: {e}")
        results.append(r)
        print(f"[{r['status']:<5}] {r['test']:<32} {r['summary']}")
    overall = worst([r["status"] for r in results])
    print("-" * 78 + f"\nOVERALL: {overall}  (heuristic thresholds; see excluded tests before any claim of anonymity)")

    sha = hashlib.sha256(open(a.csv, "rb").read()).hexdigest()
    report = dict(generated_utc=datetime.now(timezone.utc).isoformat(), dataset=a.csv, dataset_sha256=sha,
                  rows=len(df), columns=[c for c in df.columns if not c.startswith("_")],
                  parameters={k: v for k, v in vars(a).items()}, python=platform.python_version(),
                  pandas=pd.__version__, numpy=np.__version__, overall=overall, results=results)
    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    print(f"Full report: {a.out}")
    sys.exit(2 if overall in ("FAIL", "ERROR") else 0)


if __name__ == "__main__":
    main()
