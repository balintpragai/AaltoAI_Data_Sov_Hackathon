#!/usr/bin/env python3
"""
Anonymize the mobile-network dataset described in "Dataset description.txt",
applying the three-criteria framework from the EDPB Guidelines 02/2026 on
Anonymisation (No Record Isolation, No Linkage, No Inference).

Pipeline
--------
1. Drop direct identifiers (msisdn, imsi) -- GDPR Art. 4(1) identifiers that
   let an entity single out a record with no further processing at all.
2. Generalize imei to its TAC (first 8 digits) -- the TAC identifies a device
   model/manufacturer shared by many handsets, not one handset, so it no
   longer functions as a per-subscriber identifier (guideline paras 23-25 on
   attributes vs. identifiers).
3. Generalize time_start from a Unix timestamp (seconds) to hours since
   epoch -- reduces temporal precision, one of the factors the guidelines
   name as increasing identifiability (para 29a: "the accuracy and precision
   of the information").
4. Round high-precision throughput/RTT/volume metrics to a fixed number of
   significant figures -- same precision factor (para 29a); coarsens the
   near-unique float fingerprints that make individual records isolable.
5. Enforce k-anonymity (No Record Isolation, section 3.4.1) over the
   remaining quasi-identifiers (time bucket, province, radio_access_type,
   enb) by suppressing records whose combination occurs fewer than k times.

Usage:
    python guide_anonymize.py input.csv output.csv
    python guide_anonymize.py input.csv output.csv --k-min 10 --sig-figs 3
    python guide_anonymize.py input.csv output.csv --report outputs/report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# --- Schema, per Dataset description.txt --------------------------------

DIRECT_IDENTIFIERS = ["msisdn", "imsi"]
IMEI_COL = "imei"
TIME_COL = "time_start"
ENB_CANDIDATES = ("enb", "enb_id")
BASE_QI_COLUMNS = ["province", "radio_access_type"]

VOLUME_COLUMNS = [
    "data_GB_sum",
    "im_video_GB_sum",
    "im_audio_GB_sum",
    "tethering_data_GB_dl_sum",
]
METRIC_COLUMNS = [
    "tp_dl_avg",
    "tp_ul_avg",
    "tp_dl_filtered_avg",
    "cont_rtt_radio_avg",
    "cont_rtt_internet_avg",
    "initial_rtt_radio_avg",
    "tcp_retrans_byte_ratio_downlink_avg",
    "tcp_retrans_byte_ratio_uplink_avg",
    "http_response_time_avg",
    "http_sr_avg",
]

IMEI_TAC_LEN = 8


def find_enb_col(df: pd.DataFrame) -> str | None:
    for c in ENB_CANDIDATES:
        if c in df.columns:
            return c
    return None


def drop_direct_identifiers(df: pd.DataFrame, report: dict) -> pd.DataFrame:
    present = [c for c in DIRECT_IDENTIFIERS if c in df.columns]
    report["direct_identifiers_removed"] = present
    return df.drop(columns=present, errors="ignore")


def generalize_imei_to_tac(df: pd.DataFrame, report: dict) -> pd.DataFrame:
    if IMEI_COL not in df.columns:
        report["imei_generalized_to_tac"] = False
        return df
    imei = df[IMEI_COL].astype(str).str.strip()
    tac = imei.str[:IMEI_TAC_LEN]
    tac = tac.where(df[IMEI_COL].notna() & (imei != "nan") & (imei != ""))
    df = df.copy()
    df[IMEI_COL] = tac
    report["imei_generalized_to_tac"] = {"kept_digits": IMEI_TAC_LEN}
    return df


def generalize_time_to_hours(df: pd.DataFrame, report: dict) -> pd.DataFrame:
    if TIME_COL not in df.columns:
        report["time_start_generalized_to_hours"] = False
        return df
    ts = pd.to_numeric(df[TIME_COL], errors="coerce")
    df = df.copy()
    df[TIME_COL] = np.floor(ts / 3600).astype("Int64")
    report["time_start_generalized_to_hours"] = True
    return df


def _round_sig(values: pd.Series, sig: int) -> pd.Series:
    """Round a numeric series to `sig` significant figures (not decimal places),
    so both tiny throughput fractions and larger RTT values get coarsened
    consistently."""
    x = pd.to_numeric(values, errors="coerce")
    nonzero = x != 0
    magnitude = pd.Series(np.zeros(len(x)), index=x.index)
    magnitude[nonzero] = np.floor(np.log10(x[nonzero].abs()))
    factor = 10.0 ** (sig - 1 - magnitude)
    return (x * factor).round() / factor


def round_numeric_precision(df: pd.DataFrame, report: dict, sig_figs: int) -> pd.DataFrame:
    cols = [c for c in VOLUME_COLUMNS + METRIC_COLUMNS if c in df.columns]
    df = df.copy()
    for c in cols:
        df[c] = _round_sig(df[c], sig_figs)
    report["numeric_columns_rounded"] = {"columns": cols, "significant_figures": sig_figs}
    return df


def enforce_k_anonymity(df: pd.DataFrame, report: dict, k_min: int) -> pd.DataFrame:
    """No Record Isolation (guideline section 3.4.1): suppress any record whose
    quasi-identifier combination is shared by fewer than k_min other records,
    since a small enough class lets an entity single out those individuals."""
    enb_col = find_enb_col(df)
    qi_cols = [c for c in BASE_QI_COLUMNS if c in df.columns]
    if TIME_COL in df.columns:
        qi_cols = [TIME_COL] + qi_cols
    if enb_col:
        qi_cols = qi_cols + [enb_col]

    if not qi_cols:
        report["k_anonymity"] = {"skipped": "no quasi-identifier columns present"}
        return df

    class_size = df.groupby(qi_cols, dropna=False)[qi_cols[0]].transform("size")
    below_k = class_size < k_min
    n_dropped = int(below_k.sum())

    report["k_anonymity"] = {
        "qi_columns": qi_cols,
        "k_min": k_min,
        "rows_dropped": n_dropped,
        "rows_kept": int(len(df) - n_dropped),
    }
    return df.loc[~below_k].copy()


def anonymize(input_file: str, k_min: int, sig_figs: int) -> tuple[pd.DataFrame, dict]:
    df = pd.read_csv(input_file, dtype={c: str for c in DIRECT_IDENTIFIERS + [IMEI_COL]})
    report: dict = {"input_file": str(input_file), "input_rows": len(df)}

    df = drop_direct_identifiers(df, report)
    df = generalize_imei_to_tac(df, report)
    df = generalize_time_to_hours(df, report)
    df = round_numeric_precision(df, report, sig_figs)
    df = enforce_k_anonymity(df, report, k_min)

    report["output_rows"] = len(df)
    return df, report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Anonymize the mobile-network CSV per the EDPB Guidelines 02/2026 three-criteria framework."
    )
    parser.add_argument("input_file", help="Path to the raw input CSV")
    parser.add_argument("output_file", help="Path/name for the anonymized output CSV (written under outputs/)")
    parser.add_argument("--k-min", type=int, default=10, help="Minimum equivalence-class size (default: 10)")
    parser.add_argument(
        "--sig-figs", type=int, default=3, help="Significant figures to keep in numeric metric columns (default: 3)"
    )
    parser.add_argument("--report", help="Path for the JSON documentation report (default: outputs/<name>_report.json)")
    args = parser.parse_args()

    try:
        df, report = anonymize(args.input_file, args.k_min, args.sig_figs)

        dest = Path("outputs") / Path(args.output_file).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(dest, index=False)
        report["output_file"] = str(dest)

        report_path = Path(args.report) if args.report else dest.with_name(dest.stem + "_report.json")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2))

        print(f"Anonymized {report['input_rows']} rows -> {report['output_rows']} rows.")
        print(f"Output: {dest}")
        print(f"Report: {report_path}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
