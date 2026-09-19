"""Data-utility metrics: original vs anonymised mobile telemetry.

RUN BY A HUMAN in the controlled environment (AGENTS.md 1.4). Reads both full CSVs.
Outputs only aggregate statistics and charts (no row-level data) to --out.

Metrics (numbering follows the agreed list):
  1  Row retention
  3  KPI completeness (non-null rate per column)
  4  Distribution fidelity (KS, median / P95 error)
  5  Categorical fidelity (Jensen-Shannon divergence)
  6  Correlation preservation (Spearman matrices)
  7  Known-finding reproduction (RAT throughput gap, province ranking)
  8  Downstream ML utility, train-on-anonymised / test-on-original (TSTR)
  9  Cell-level degradation detection (top-5% worst cells recall)

Direct identifiers (msisdn, imsi, imei) are never loaded from the original file.
Device type: anonymised imei (constant TAC) + random int 0..20 -> `device_type`.
The original gets the same column (copied if row counts match, otherwise
resampled from the anonymised column), so the comparison is fair.

Usage:
  python src/utility_metrics.py --original <orig.csv> --anonymized <anon.csv> --out outputs/utility_report
  python src/utility_metrics.py --synthetic --out <dir>      # self-test on fake data
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import ks_2samp, spearmanr
from scipy.spatial.distance import jensenshannon
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import f1_score, r2_score

PII = ["msisdn", "imsi", "imei"]
KPI = [
    "tp_dl_avg", "tp_ul_avg", "tp_dl_filtered_avg",
    "cont_rtt_radio_avg", "cont_rtt_internet_avg", "initial_rtt_radio_avg",
    "tcp_retrans_byte_ratio_downlink_avg", "tcp_retrans_byte_ratio_uplink_avg",
    "http_response_time_avg", "http_sr_avg",
    "data_GB_sum", "im_video_GB_sum", "im_audio_GB_sum", "tethering_data_GB_dl_sum",
]
CAT = ["radio_access_type", "province", "application_category", "device_type"]
ORIG_C, ANON_C = "#4C78A8", "#F58518"
OK_C, BAD_C, SKIP_C = "#2E9E5B", "#D64545", "#8A8A8A"


# ----------------------------------------------------------------- loading
def load_original(path):
    header = pd.read_csv(path, nrows=0).columns
    keep = [c for c in header if c not in PII and c != "time_start"]  # PII never loaded
    df = pd.read_csv(path, usecols=keep, dtype={"enb": str, "enb_id": str})
    return df.rename(columns={"enb": "enb_id"})


def load_anon(path):
    header = pd.read_csv(path, nrows=0).columns
    wanted = set(KPI) | set(CAT) | {"enb_id", "imei"}
    df = pd.read_csv(path, usecols=[c for c in header if c in wanted], dtype={"enb_id": str})
    return df


def synthetic(seed):
    """SYNTHETIC data only. Returns (orig without PII, anon with constant imei)."""
    rng = np.random.default_rng(seed)
    n = 30000
    rat = rng.choice(["2G", "4G", "5G"], n, p=[0.1, 0.6, 0.3])
    base = pd.Series(rat).map({"2G": 0.05, "4G": 0.3, "5G": 1.0}).values
    tp = base * rng.lognormal(0, 0.7, n)
    df = pd.DataFrame({
        "tp_dl_avg": tp,
        "tp_ul_avg": tp * rng.uniform(0.1, 0.4, n),
        "tp_dl_filtered_avg": tp * 1.5,
        "cont_rtt_radio_avg": 200 / (1 + 3 * base) * rng.lognormal(0, 0.3, n),
        "cont_rtt_internet_avg": 100 * rng.lognormal(0, 0.4, n),
        "initial_rtt_radio_avg": 30 * rng.lognormal(0, 0.3, n),
        "tcp_retrans_byte_ratio_downlink_avg": rng.beta(1, 60, n),
        "tcp_retrans_byte_ratio_uplink_avg": rng.beta(1, 80, n),
        "http_response_time_avg": rng.lognormal(0, 0.5, n),
        "http_sr_avg": rng.uniform(0.9, 1, n),
        "data_GB_sum": tp * 0.01, "im_video_GB_sum": tp * 0.004,
        "im_audio_GB_sum": tp * 0.001, "tethering_data_GB_dl_sum": tp * 0.0005,
        "radio_access_type": rat,
        "province": rng.choice(["Uusimaa", "Pirkanmaa", "Lappi", "Savo"], n),
        "application_category": rng.choice(["AI", "P2P", "VIDEO", "WEB"], n),
        "enb_id": rng.integers(85117000, 85117200, n).astype(str),
    })
    df.loc[rng.random(n) < 0.3, "tp_dl_filtered_avg"] = np.nan
    anon = df.sample(frac=0.97, random_state=seed).reset_index(drop=True)
    anon["cont_rtt_radio_avg"] *= rng.normal(1, 0.02, len(anon))
    anon["imei"] = 10000000
    return df, anon


# ----------------------------------------------------------------- helpers
def sample(s, n, rng):
    return s if len(s) <= n else s.iloc[rng.choice(len(s), n, replace=False)]


def rel_err(o, a):
    if o == 0 and a == 0:
        return 0.0
    return abs(a - o) / max(abs(o), 1e-12)


def tag(ok):
    return "PASS" if ok else "FAIL"


def title_color(ok):
    return OK_C if ok else BAD_C


def save(fig, out, name):
    fig.tight_layout()
    fig.savefig(out / name, dpi=130)
    plt.close(fig)


# ----------------------------------------------------------------- metrics
def m1_rows(orig, anon, out, detail):
    r = len(anon) / len(orig)
    ok = r >= 0.95
    detail.append(dict(metric=1, item="row_retention", value=r, target=">=0.95", status=tag(ok)))
    fig, ax = plt.subplots(figsize=(6, 2.6))
    ax.barh(["Original", "Anonymised"], [len(orig), len(anon)], color=[ORIG_C, ANON_C])
    ax.set_title(f"M1 Row retention: {r:.1%} (target ≥ 95%) - {tag(ok)}", color=title_color(ok))
    ax.set_xlabel("rows")
    save(fig, out, "m1_row_retention.png")
    return dict(id="M1", name="Row retention", baseline="100%", target=">= 95%", result=f"{r:.1%}", status=tag(ok))


def m3_completeness(orig, anon, out, detail):
    rows = []
    for c in KPI:
        if c in orig and c in anon:
            po, pa = orig[c].notna().mean(), anon[c].notna().mean()
            if po > 0:
                rows.append((c, po, pa, pa / po))
    d = pd.DataFrame(rows, columns=["col", "orig", "anon", "ratio"])
    ok_all = bool((d.ratio >= 0.95).all())
    for _, r in d.iterrows():
        detail.append(dict(metric=3, item=r.col, value=r.ratio, target=">=0.95", status=tag(r.ratio >= 0.95)))
    fig, ax = plt.subplots(figsize=(9, 5))
    y = np.arange(len(d))
    ax.barh(y - 0.2, d.orig, 0.4, color=ORIG_C, label="Original")
    ax.barh(y + 0.2, d.anon, 0.4, color=ANON_C, label="Anonymised")
    ax.set_yticks(y, d.col)
    ax.invert_yaxis()
    ax.set_xlabel("non-null share")
    ax.legend()
    ax.set_title(f"M3 KPI completeness (min ratio {d.ratio.min():.2f}, target ≥ 0.95) - {tag(ok_all)}",
                 color=title_color(ok_all))
    save(fig, out, "m3_completeness.png")
    return dict(id="M3", name="KPI completeness", baseline="raw non-null rate", target="ratio >= 0.95 per column",
                result=f"min ratio {d.ratio.min():.2f}", status=tag(ok_all))


def m4_distributions(orig, anon, out, detail, rng, n):
    cols = [c for c in KPI if c in orig and c in anon]
    stats = []
    for c in cols:
        o = sample(orig[c].dropna(), n, rng).values
        a = sample(anon[c].dropna(), n, rng).values
        if len(o) < 2 or len(a) < 2:
            continue
        ks = ks_2samp(o, a).statistic
        e50 = rel_err(np.median(o), np.median(a))
        e95 = rel_err(np.percentile(o, 95), np.percentile(a, 95))
        ok = ks <= 0.05 and e50 <= 0.05 and e95 <= 0.05
        stats.append((c, ks, e50, e95, ok, o, a))
        detail.append(dict(metric=4, item=c, value=ks, target="KS<=0.05, med/P95 err<=5%",
                           status=tag(ok), extra=f"ks={ks:.3f} med_err={e50:.3f} p95_err={e95:.3f}"))
    ncol = 4
    nrow = int(np.ceil(len(stats) / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(4 * ncol, 3 * nrow), squeeze=False)
    p = np.linspace(0, 1, 300)
    for ax, (c, ks, e50, e95, ok, o, a) in zip(axes.ravel(), stats):
        ax.plot(np.quantile(o, p), p, color=ORIG_C, lw=2, label="Original")
        ax.plot(np.quantile(a, p), p, color=ANON_C, lw=1.5, ls="--", label="Anonymised")
        ax.set_xscale("symlog", linthresh=1e-6)
        ax.set_title(f"{c}\nKS={ks:.3f}  {tag(ok)}", fontsize=8, color=title_color(ok))
        ax.tick_params(labelsize=7)
    for ax in axes.ravel()[len(stats):]:
        ax.axis("off")
    axes.ravel()[0].legend(fontsize=7)
    fig.suptitle("M4 Distribution fidelity (cumulative distributions overlap = good)")
    save(fig, out, "m4_distributions.png")
    ok_all = all(s[4] for s in stats)
    worst = max(stats, key=lambda s: s[1])
    return dict(id="M4", name="Distribution fidelity", baseline="KS = 0", target="KS <= 0.05, med/P95 err <= 5%",
                result=f"max KS {worst[1]:.3f} ({worst[0]})", status=tag(ok_all))


def m5_categorical(orig, anon, out, detail):
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    worst, oks = 0.0, []
    for ax, c in zip(axes.ravel(), CAT):
        po = orig[c].astype(str).value_counts(normalize=True)
        pa = anon[c].astype(str).value_counts(normalize=True)
        idx = po.index.union(pa.index)
        po, pa = po.reindex(idx, fill_value=0), pa.reindex(idx, fill_value=0)
        jsd = float(jensenshannon(po, pa, base=2) ** 2)
        ok = jsd <= 0.02
        oks.append(ok)
        worst = max(worst, jsd)
        detail.append(dict(metric=5, item=c, value=jsd, target="<=0.02", status=tag(ok)))
        order = po.sort_values(ascending=False).index[:15]
        x = np.arange(len(order))
        ax.bar(x - 0.2, po[order], 0.4, color=ORIG_C, label="Original")
        ax.bar(x + 0.2, pa[order], 0.4, color=ANON_C, label="Anonymised")
        ax.set_xticks(x, order, rotation=60, ha="right", fontsize=7)
        ax.set_title(f"{c}: JSD={jsd:.4f}  {tag(ok)}", fontsize=9, color=title_color(ok))
    axes[0, 0].legend()
    fig.suptitle("M5 Categorical fidelity (share of rows per category)")
    save(fig, out, "m5_categorical.png")
    return dict(id="M5", name="Categorical fidelity", baseline="JSD = 0", target="JSD <= 0.02",
                result=f"max JSD {worst:.4f}", status=tag(all(oks)))


def m6_correlation(orig, anon, out, detail, rng, n):
    cols = [c for c in KPI if c in orig and c in anon]
    co = sample(orig[cols], n, rng).corr(method="spearman")
    ca = sample(anon[cols], n, rng).corr(method="spearman")
    diff = (ca - co).abs()
    worst = float(np.nanmax(diff.values)) if diff.notna().any().any() else float("nan")
    ok = worst <= 0.05
    detail.append(dict(metric=6, item="max_abs_delta_rho", value=worst, target="<=0.05", status=tag(ok)))
    fig, axes = plt.subplots(1, 3, figsize=(20, 6.5))
    for ax, m, t, cmap, lim in [(axes[0], co, "Original", "coolwarm", 1), (axes[1], ca, "Anonymised", "coolwarm", 1),
                                (axes[2], diff, "|Difference|", "Reds", 0.2)]:
        im = ax.imshow(m.values, cmap=cmap, vmin=-lim if cmap == "coolwarm" else 0, vmax=lim)
        ax.set_xticks(range(len(cols)), cols, rotation=90, fontsize=7)
        ax.set_yticks(range(len(cols)), cols, fontsize=7)
        ax.set_title(t)
        fig.colorbar(im, ax=ax, shrink=0.7)
    fig.suptitle(f"M6 Spearman correlation preservation: max |Δρ| = {worst:.3f} (target ≤ 0.05) - {tag(ok)}",
                 color=title_color(ok))
    save(fig, out, "m6_correlation.png")
    return dict(id="M6", name="Correlation preservation", baseline="Δρ = 0", target="max |Δρ| <= 0.05",
                result=f"max |Δρ| {worst:.3f}", status=tag(ok))


def m7_findings(orig, anon, out, detail):
    t = "tp_dl_avg"
    mo = orig.groupby("radio_access_type")[t].median()
    ma = anon.groupby("radio_access_type")[t].median()
    rats = mo.index.intersection(ma.index)
    hi, lo = mo[rats].idxmax(), mo[rats].idxmin()
    gap_o, gap_a = mo[hi] - mo[lo], ma[hi] - ma[lo]
    gap_err = rel_err(gap_o, gap_a)
    po = orig.groupby("province")[t].agg(["median", "count"])
    pa = anon.groupby("province")[t].agg(["median", "count"])
    prov = po[po["count"] >= 30].index.intersection(pa[pa["count"] >= 30].index)
    rho = float(spearmanr(po.loc[prov, "median"], pa.loc[prov, "median"])[0]) if len(prov) >= 3 else float("nan")
    ok = gap_err <= 0.05 and (np.isnan(rho) or rho >= 0.95)
    detail.append(dict(metric=7, item=f"{hi}-{lo} gap rel err", value=gap_err, target="<=0.05", status=tag(gap_err <= 0.05)))
    detail.append(dict(metric=7, item="province ranking spearman", value=rho, target=">=0.95", status=tag(np.isnan(rho) or rho >= 0.95)))
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    x = np.arange(len(rats))
    axes[0].bar(x - 0.2, mo[rats], 0.4, color=ORIG_C, label="Original")
    axes[0].bar(x + 0.2, ma[rats], 0.4, color=ANON_C, label="Anonymised")
    axes[0].set_xticks(x, rats)
    axes[0].set_ylabel("median tp_dl_avg")
    axes[0].set_title(f"Throughput by radio access type\n{hi}-{lo} gap error {gap_err:.1%}")
    axes[0].legend()
    if len(prov) >= 3:
        axes[1].scatter(po.loc[prov, "median"], pa.loc[prov, "median"], color=ANON_C)
        lim = [0, max(po.loc[prov, "median"].max(), pa.loc[prov, "median"].max()) * 1.05]
        axes[1].plot(lim, lim, color="grey", ls="--", label="perfect match")
        axes[1].set_xlabel("Original province median tp_dl_avg")
        axes[1].set_ylabel("Anonymised")
        axes[1].legend()
    axes[1].set_title(f"Province ranking preserved: Spearman ρ = {rho:.3f}")
    fig.suptitle(f"M7 Known findings reproduced - {tag(ok)}", color=title_color(ok))
    save(fig, out, "m7_known_findings.png")
    return dict(id="M7", name="Known-finding reproduction", baseline="raw values",
                target="RAT gap within ±5%, province ρ >= 0.95", result=f"gap err {gap_err:.1%}, ρ {rho:.2f}",
                status=tag(ok))


def m8_tstr(orig, anon, out, detail, rng, n, seed):
    target = "tp_dl_avg"
    num = [c for c in KPI if c not in (target, "tp_dl_filtered_avg") and c in orig and c in anon]
    maps = {}
    for c in CAT:
        cats = sorted(set(orig[c].dropna().astype(str)) | set(anon[c].dropna().astype(str)))
        maps[c] = {v: i for i, v in enumerate(cats)}

    def enc(df):
        X = df[num].copy()
        for c in CAT:
            X[c] = df[c].astype(str).map(maps[c]).fillna(-1).astype(int)
        return X

    o = sample(orig[orig[target].notna()], n, rng)
    a = sample(anon[anon[target].notna()], n, rng)
    perm = rng.permutation(len(o))
    cut = int(len(o) * 0.7)
    tr, te = o.iloc[perm[:cut]], o.iloc[perm[cut:]]
    mask = [False] * len(num) + [True] * len(CAT)
    thr = tr[target].quantile(0.2)  # "poor QoE" = bottom 20% throughput

    res = {}
    for name, train in [("Trained on original", tr), ("Trained on anonymised", a)]:
        reg = HistGradientBoostingRegressor(categorical_features=mask, random_state=seed, max_iter=200)
        reg.fit(enc(train), np.log1p(train[target]))
        pred = reg.predict(enc(te))
        clf = HistGradientBoostingClassifier(categorical_features=mask, random_state=seed, max_iter=200)
        clf.fit(enc(train), (train[target] < thr).astype(int))
        f1 = f1_score((te[target] < thr).astype(int), clf.predict(enc(te)))
        res[name] = dict(r2=r2_score(np.log1p(te[target]), pred), f1=f1, pred=pred)
    ro, ra = res["Trained on original"], res["Trained on anonymised"]
    r2_ratio = ra["r2"] / ro["r2"] if ro["r2"] > 0 else float("nan")
    f1_ratio = ra["f1"] / ro["f1"] if ro["f1"] > 0 else float("nan")
    ok = r2_ratio >= 0.95 and f1_ratio >= 0.90
    detail.append(dict(metric=8, item="R2 ratio anon/orig", value=r2_ratio, target=">=0.95", status=tag(r2_ratio >= 0.95),
                       extra=f"orig={ro['r2']:.3f} anon={ra['r2']:.3f}"))
    detail.append(dict(metric=8, item="F1 ratio anon/orig", value=f1_ratio, target=">=0.90", status=tag(f1_ratio >= 0.90),
                       extra=f"orig={ro['f1']:.3f} anon={ra['f1']:.3f}"))

    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    x = np.arange(2)
    axes[0].bar(x - 0.2, [ro["r2"], ro["f1"]], 0.4, color=ORIG_C, label="Trained on original (baseline)")
    axes[0].bar(x + 0.2, [ra["r2"], ra["f1"]], 0.4, color=ANON_C, label="Trained on anonymised")
    axes[0].set_xticks(x, ["R² (log throughput)", "F1 (poor-QoE flag)"])
    axes[0].set_ylim(0, 1)
    axes[0].legend(fontsize=8)
    axes[0].set_title(f"R² kept {r2_ratio:.0%} (≥95%), F1 kept {f1_ratio:.0%} (≥90%)")
    idx = rng.choice(len(te), min(3000, len(te)), replace=False)
    truth = np.log1p(te[target].values)[idx]
    for ax, (nm, r), col in zip(axes[1:], res.items(), [ORIG_C, ANON_C]):
        ax.scatter(truth, r["pred"][idx], s=4, alpha=0.4, color=col)
        lim = [min(truth.min(), r["pred"][idx].min()), max(truth.max(), r["pred"][idx].max())]
        ax.plot(lim, lim, color="grey", ls="--")
        ax.set_xlabel("actual log1p(tp_dl_avg) on ORIGINAL test data")
        ax.set_ylabel("predicted")
        ax.set_title(f"{nm}: R² = {r['r2']:.3f}")
    fig.suptitle(f"M8 Downstream ML utility (train on X, test on held-out original) - {tag(ok)}",
                 color=title_color(ok))
    save(fig, out, "m8_ml_utility.png")
    return dict(id="M8", name="Downstream ML utility (TSTR)", baseline=f"orig-trained R² {ro['r2']:.2f}, F1 {ro['f1']:.2f}",
                target="R² >= 95%, F1 >= 90% of baseline", result=f"R² {r2_ratio:.0%}, F1 {f1_ratio:.0%}", status=tag(ok))


def m9_cells(orig, anon, out, detail, min_rows=20, top=0.05):
    base = dict(id="M9", name="Cell degradation detection", baseline="100% recall", target="recall >= 90%")
    o_ids, a_ids = set(orig["enb_id"].dropna()) - {"0"}, set(anon["enb_id"].dropna()) - {"0"}
    common = o_ids & a_ids
    if len(common) < 0.01 * max(len(a_ids), 1) or len(common) < 20:
        return dict(**base, result="enb_id not comparable", status="SKIP",
                    note=f"only {len(common)} shared cell ids; anonymised enb_id looks re-mapped/generalised. Provide a mapping to run M9.")
    signals = [("tcp_retrans_byte_ratio_downlink_avg", "TCP retrans DL"), ("cont_rtt_radio_avg", "Radio RTT")]
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))
    recalls = []
    for ax, (col, label) in zip(axes, signals):
        go = orig[orig.enb_id.isin(common)].groupby("enb_id")[col].agg(["mean", "count"])
        ga = anon[anon.enb_id.isin(common)].groupby("enb_id")[col].agg(["mean", "count"])
        cells = go[go["count"] >= min_rows].index.intersection(ga[ga["count"] >= min_rows].index)
        mo, ma = go.loc[cells, "mean"], ga.loc[cells, "mean"]
        k = max(int(len(cells) * top), 1)
        to, ta = set(mo.nlargest(k).index), set(ma.nlargest(k).index)
        rec = len(to & ta) / len(to)
        recalls.append(rec)
        detail.append(dict(metric=9, item=f"recall {col}", value=rec, target=">=0.90", status=tag(rec >= 0.9)))
        hit = mo.index.isin(to)
        ax.scatter(mo[~hit], ma[~hit], s=8, color="#B8B8B8", label="other cells")
        ax.scatter(mo[hit], ma[hit], s=18, color=ANON_C, label="worst 5% in original")
        ax.set_xscale("symlog", linthresh=1e-6)
        ax.set_yscale("symlog", linthresh=1e-6)
        ax.set_xlabel(f"Original cell mean {label}")
        ax.set_ylabel("Anonymised")
        ax.set_title(f"{label}: recall of worst cells = {rec:.0%}", color=title_color(rec >= 0.9))
        ax.legend(fontsize=8)
    ok = min(recalls) >= 0.9
    fig.suptitle(f"M9 Worst-cell detection (cells with ≥{min_rows} rows) - {tag(ok)}", color=title_color(ok))
    save(fig, out, "m9_cell_detection.png")
    return dict(**base, result=f"recall {min(recalls):.0%} (min of 2 signals)", status=tag(ok))


# ----------------------------------------------------------------- scorecard
def scorecard(rows, out):
    fig, ax = plt.subplots(figsize=(15, 0.7 * len(rows) + 1.6))
    ax.axis("off")
    heads = ["", "Metric", "Baseline", "Target", "Result", "Status"]
    xs = [0.0, 0.05, 0.28, 0.5, 0.75, 0.93]
    for x, h in zip(xs, heads):
        ax.text(x, 1, h, fontweight="bold", fontsize=11, va="top", transform=ax.transAxes)
    step = 0.9 / max(len(rows), 1)
    for i, r in enumerate(rows):
        y = 0.92 - i * step
        col = {"PASS": OK_C, "FAIL": BAD_C}.get(r["status"], SKIP_C)
        for x, v in zip(xs[:5], [r["id"], r["name"], r["baseline"], r["target"], r["result"]]):
            ax.text(x, y, v, fontsize=10, va="top", transform=ax.transAxes)
        ax.text(xs[5], y, r["status"], fontsize=11, fontweight="bold", color="white", va="top",
                bbox=dict(boxstyle="round,pad=0.3", fc=col, ec=col), transform=ax.transAxes)
    ax.set_title("Data utility scorecard - anonymised vs original", fontsize=14, loc="left")
    save(fig, out, "00_scorecard.png")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--original")
    ap.add_argument("--anonymized")
    ap.add_argument("--out", default="outputs/utility_report")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-rows", type=int, default=300000, help="row cap for KS / correlation / ML sampling")
    ap.add_argument("--synthetic", action="store_true", help="self-test with fake data")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles (cp1250) can't print Δ/ρ
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)

    if args.synthetic:
        orig, anon = synthetic(args.seed)
    else:
        if not (args.original and args.anonymized):
            ap.error("--original and --anonymized are required (or use --synthetic)")
        orig, anon = load_original(args.original), load_anon(args.anonymized)

    # device type: anonymised imei (constant TAC) + random 0..20; identical column used for the original
    anon["device_type"] = (anon["imei"].astype("int64") + rng.integers(0, 21, len(anon))).astype(str)
    anon = anon.drop(columns=["imei"])
    if len(orig) == len(anon):
        orig["device_type"] = anon["device_type"].values
        dev_note = "copied row-for-row from anonymised file"
    else:
        orig["device_type"] = rng.choice(anon["device_type"].values, len(orig))
        dev_note = "row counts differ -> resampled from the anonymised device_type distribution"
    print(f"device_type: {dev_note}")

    detail, rows = [], []
    n = args.max_rows
    rows.append(m1_rows(orig, anon, out, detail))
    rows.append(m3_completeness(orig, anon, out, detail))
    rows.append(m4_distributions(orig, anon, out, detail, rng, n))
    rows.append(m5_categorical(orig, anon, out, detail))
    rows.append(m6_correlation(orig, anon, out, detail, rng, n))
    rows.append(m7_findings(orig, anon, out, detail))
    rows.append(m8_tstr(orig, anon, out, detail, rng, n, args.seed))
    rows.append(m9_cells(orig, anon, out, detail))

    scorecard(rows, out)
    pd.DataFrame(detail).to_csv(out / "metrics_detail.csv", index=False)  # aggregates only
    pd.DataFrame(rows).to_csv(out / "metrics_summary.csv", index=False)
    print(pd.DataFrame(rows)[["id", "name", "result", "status"]].to_string(index=False))
    for r in rows:
        if r.get("note"):
            print(f"NOTE {r['id']}: {r['note']}")
    print(f"\nReport written to {out.resolve()} (start with 00_scorecard.png)")


if __name__ == "__main__":
    main()
