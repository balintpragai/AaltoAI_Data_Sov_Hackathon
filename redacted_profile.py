#!/usr/bin/env python3
"""
Build a redacted column profile for a privacy review.

This is the only stats object allowed to leave the processing environment.
Do not send column_stats.py output (it stores top_value and ID plots) to a model.

Humans run this CLI on real CSVs. Automated tests use synthetic frames only.

    python redacted_profile.py input.csv --subject raw -o outputs/profile_raw.json
    python redacted_profile.py input.csv --subject release_candidate --k-min 10
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import pandas as pd

SUBJECTS = ("raw", "release_candidate")
ALLOWLIST_LABELS = frozenset({"province", "radio_access_type", "application_category"})
ENB_NAMES = frozenset({"enb", "enb_id"})
IDENT_NAME_RE = re.compile(
    r"(^|_)(msisdn|imsi|imei)(_|$)",
    re.IGNORECASE,
)
DEFAULT_SIG_FIGS = 3
CELL_BUCKETS = ("1", "2-9", ">=10")

# Meanings from AGENTS.md §2. Missing keys → description null (model must not invent).
COLUMN_DESCRIPTIONS: dict[str, str] = {
    "time_start": "First timestamp of event window",
    "msisdn": "Phone number",
    "imsi": "SIM subscriber identity",
    "imei": "Device identity",
    "tp_dl_avg": "Throughput metrics",
    "tp_ul_avg": "Throughput metrics",
    "tp_dl_filtered_avg": "Throughput metrics",
    "cont_rtt_radio_avg": "Latency metrics",
    "cont_rtt_internet_avg": "Latency metrics",
    "initial_rtt_radio_avg": "Latency metrics",
    "tcp_retrans_byte_ratio_downlink_avg": "TCP retransmission ratios",
    "tcp_retrans_byte_ratio_uplink_avg": "TCP retransmission ratios",
    "http_response_time_avg": "HTTP performance",
    "http_sr_avg": "HTTP performance",
    "data_GB_sum": "Total data volume",
    "im_video_GB_sum": "Video download volume",
    "im_audio_GB_sum": "Audio download volume",
    "tethering_data_GB_dl_sum": "Tethered-device volume",
    "radio_access_type": "Radio access type (e.g. LTE/NR)",
    "province": "Origin province",
    "application_category": "App type used",
    "enb": "eNodeB/gNodeB ID",
    "enb_id": "eNodeB/gNodeB ID",
}


def round_sig(value: float, sig: int = DEFAULT_SIG_FIGS) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    if value == 0:
        return 0.0
    magnitude = math.floor(math.log10(abs(value)))
    factor = 10.0 ** (sig - 1 - magnitude)
    return float(round(value * factor) / factor)


def profile_sha256(profile: dict) -> str:
    import hashlib

    blob = json.dumps(profile, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def is_identifier_like(col: str) -> bool:
    return bool(IDENT_NAME_RE.search(str(col)))


def is_enb(col: str) -> bool:
    return str(col).lower() in ENB_NAMES


def looks_like_flags(col: str) -> list[str]:
    flags: list[str] = []
    lower = str(col).lower()
    for token in ("msisdn", "imsi", "imei"):
        if token in lower.split("_") or lower == token:
            flags.append(token)
    return flags


def _cell_size_histogram(series: pd.Series) -> dict[str, int]:
    counts = series.dropna().astype(str)
    if counts.empty:
        return {k: 0 for k in CELL_BUCKETS}
    sizes = counts.value_counts()
    hist = {k: 0 for k in CELL_BUCKETS}
    for n in sizes.tolist():
        if n == 1:
            hist["1"] += 1
        elif n < 10:
            hist["2-9"] += 1
        else:
            hist[">=10"] += 1
    return hist


def _k_suppressed_categories(series: pd.Series, k_min: int) -> tuple[list[dict], int]:
    vc = series.dropna().astype(str).value_counts()
    kept: list[dict] = []
    rare_total = 0
    for label, count in vc.items():
        n = int(count)
        if n < k_min:
            rare_total += n
        else:
            kept.append({"label": str(label), "count": n})
    if rare_total >= k_min:
        kept.append({"label": "OTHER", "count": rare_total})
    kept.sort(key=lambda row: (-row["count"], row["label"]))
    return kept, rare_total


def profile_column(name: str, series: pd.Series, k_min: int, sig_figs: int, stripped: list[str]) -> dict:
    n = int(len(series))
    missing = int(series.isna().sum())
    non_null = series.dropna()
    base = {
        "name": name,
        "type": str(series.dtype),
        "description": COLUMN_DESCRIPTIONS.get(name),
        "count": n,
        "missing": missing,
        "missing_share": round(missing / n, 6) if n else None,
        "n_unique": int(non_null.nunique()) if not non_null.empty else 0,
    }

    if is_identifier_like(name):
        stripped.append(f"{name}: no values, extrema, or labels (identifier-like)")
        return {
            **base,
            "kind": "identifier_like",
            "looks_like": looks_like_flags(name) or ["identifier"],
        }

    if is_enb(name):
        stripped.append(f"{name}: cell IDs omitted; size histogram only")
        return {
            **base,
            "kind": "enb",
            "cell_size_histogram": _cell_size_histogram(series),
        }

    if name in ALLOWLIST_LABELS:
        cats, rare_total = _k_suppressed_categories(series, k_min)
        if rare_total:
            stripped.append(f"{name}: labels with count < {k_min} omitted or rolled into OTHER")
        return {
            **base,
            "kind": "allowlisted_categorical",
            "categories": cats,
        }

    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        x = pd.to_numeric(non_null, errors="coerce").dropna().astype(float)
        if x.empty:
            return {**base, "kind": "numeric"}
        qs = x.quantile([0.25, 0.5, 0.75, 0.99])
        stripped.append(f"{name}: raw min/max replaced with {sig_figs}-sig-fig rounding")
        return {
            **base,
            "kind": "numeric",
            "p25": round_sig(float(qs[0.25]), sig_figs),
            "p50": round_sig(float(qs[0.5]), sig_figs),
            "p75": round_sig(float(qs[0.75]), sig_figs),
            "p99": round_sig(float(qs[0.99]), sig_figs),
            "min_rounded": round_sig(float(x.min()), sig_figs),
            "max_rounded": round_sig(float(x.max()), sig_figs),
        }

    stripped.append(f"{name}: non-allowlisted categorical; labels omitted")
    return {**base, "kind": "categorical_nolabels"}


def build_redacted_profile(
    df: pd.DataFrame,
    subject: str,
    k_min: int = 10,
    sig_figs: int = DEFAULT_SIG_FIGS,
) -> dict:
    if subject not in SUBJECTS:
        raise ValueError(f"subject must be one of {SUBJECTS}")
    if k_min < 2:
        raise ValueError("k_min must be >= 2")
    stripped: list[str] = []
    columns = [profile_column(str(c), df[c], k_min, sig_figs, stripped) for c in df.columns]
    profile = {
        "subject": subject,
        "k_min": k_min,
        "sig_figs": sig_figs,
        "row_count": int(len(df)),
        "column_count": int(df.shape[1]),
        "redaction": {
            "k_min": k_min,
            "allowlisted_label_columns": sorted(ALLOWLIST_LABELS),
            "stripped": stripped,
        },
        "columns": columns,
    }
    profile["profile_sha256"] = profile_sha256({k: v for k, v in profile.items() if k != "profile_sha256"})
    return profile


def assert_no_forbidden_payload(profile: dict, forbidden_substrings: list[str] | None = None) -> None:
    """Fail if identifier-like values or enb IDs leaked into the JSON."""
    blob = json.dumps(profile)
    if "top_value" in blob:
        raise AssertionError("redacted profile must not contain top_value")
    for col in profile["columns"]:
        if col["kind"] in {"identifier_like", "enb", "categorical_nolabels"}:
            if "categories" in col or "top_value" in col:
                raise AssertionError(f"{col['name']} must not carry category labels")
    if forbidden_substrings:
        for s in forbidden_substrings:
            if s and s in blob:
                raise AssertionError(f"forbidden value leaked into profile: {s!r}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Write a redacted column profile (human-run on real CSVs).")
    p.add_argument("csv", help="Input CSV (human-approved path)")
    p.add_argument("--subject", required=True, choices=SUBJECTS)
    p.add_argument("--k-min", type=int, default=10)
    p.add_argument("--sig-figs", type=int, default=DEFAULT_SIG_FIGS)
    p.add_argument("-o", "--output", default="", help="JSON path (default: outputs/profile_<subject>.json)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    df = pd.read_csv(args.csv)
    profile = build_redacted_profile(df, args.subject, args.k_min, args.sig_figs)
    assert_no_forbidden_payload(profile)
    dest = Path(args.output) if args.output else Path("outputs") / f"profile_{args.subject}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(profile, indent=2))
    print(f"Wrote {dest} sha256={profile['profile_sha256']}")
    print("Do not commit this file. Keep it under outputs/ (gitignored).")


if __name__ == "__main__":
    main()
