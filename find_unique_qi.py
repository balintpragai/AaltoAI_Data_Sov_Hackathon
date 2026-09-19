#!/usr/bin/env python3
"""
Find rows that fail T3 k-anonymity because their QI combination is unique (k=1).

Default QIs match the failing "all base QIs" set in privacy_report.json:
    _hour, province, radio_access_type

Usage:
    python find_unique_qi.py out.csv
    python find_unique_qi.py out.csv --k-max 9
    python find_unique_qi.py out.csv --qi _hour province radio_access_type
    python find_unique_qi.py out.csv --fix
    python find_unique_qi.py out.csv --k-max 9 --fix --output outputs/out_k10.csv

--fix prompts for drop vs anonymize. Anonymize asks which column to set to
RESTRICTED on small-class rows only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from tests import TIME, prep


def find_small_classes(df: pd.DataFrame, qi: list[str], k_max: int) -> pd.DataFrame:
    missing = [c for c in qi if c not in df.columns]
    if missing:
        raise SystemExit(f"QI column(s) not in data: {', '.join(missing)}")

    work = df.copy()
    work["_row"] = work.index
    grouped = work.groupby(qi, dropna=False, observed=True, sort=False)
    sizes = grouped.size().rename("k")
    small = sizes[sizes <= k_max].reset_index()
    if small.empty:
        return small

    members = grouped["_row"].apply(list).rename("row_indices")
    out = small.merge(members.reset_index(), on=qi, how="left")
    return out.sort_values(["k", *qi], kind="mergesort").reset_index(drop=True)


HELPER_COLS = ("_ts", "_hour", "_hod")


def small_class_mask(df: pd.DataFrame, qi: list[str], k_max: int) -> pd.Series:
    k = df.groupby(qi, dropna=False, observed=True, sort=False)[qi[0]].transform("size")
    return k <= k_max


def drop_small_classes(df: pd.DataFrame, qi: list[str], k_max: int) -> pd.DataFrame:
    return df.loc[~small_class_mask(df, qi, k_max)].copy()


def anonymize_small_classes(df: pd.DataFrame, qi: list[str], k_max: int, column: str) -> pd.DataFrame:
    if column not in df.columns:
        raise SystemExit(f"Column not in data: {column}")
    if column in HELPER_COLS:
        raise SystemExit(f"Cannot anonymize helper column {column}")
    out = df.copy()
    out.loc[small_class_mask(out, qi, k_max), column] = "RESTRICTED"
    return out


def ask_fix_action() -> str:
    while True:
        choice = input("Fix small classes by dropping rows or anonymization? [drop/anonymize]: ").strip().lower()
        if choice in ("drop", "d", "dropping"):
            return "drop"
        if choice in ("anonymize", "anonymization", "a"):
            return "anonymize"
        print("Please enter 'drop' or 'anonymize'.")


def ask_anonymize_column(df: pd.DataFrame) -> str:
    candidates = [c for c in df.columns if c not in HELPER_COLS]
    print("Available columns:", ", ".join(candidates))
    while True:
        column = input("Which column should be anonymized in small-class rows? ").strip()
        if column in candidates:
            return column
        print(f"Unknown column {column!r}. Choose one of: {', '.join(candidates)}")


def write_release(df: pd.DataFrame, path: str) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = df.drop(columns=[c for c in HELPER_COLS if c in df.columns], errors="ignore")
    out.to_csv(dest, index=False)


def main() -> None:
    p = argparse.ArgumentParser(description="List QI combinations that create uniqueness (small k).")
    p.add_argument("csv", help="Release-candidate CSV (e.g. out.csv)")
    p.add_argument(
        "--qi",
        nargs="+",
        default=["_hour", "province", "radio_access_type"],
        help="Quasi-identifier columns (default: _hour province radio_access_type)",
    )
    p.add_argument("--tz", default="UTC", help="Timezone used to bucket time_start into _hour")
    p.add_argument(
        "--k-max",
        type=int,
        default=1,
        help="Report classes with k <= this value (default 1: unique QI tuples that fail T3)",
    )
    p.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    p.add_argument(
        "--fix",
        action="store_true",
        help="Interactively drop small-class rows or anonymize a chosen column in those rows",
    )
    p.add_argument(
        "--output",
        help="CSV path for --fix (default: outputs/<input filename>)",
    )
    args = p.parse_args()

    df = pd.read_csv(args.csv)
    df = prep(df, args.tz)
    df = df[df["province"] != "Unavailable"].copy()

    if "_hour" in args.qi and "_hour" not in df.columns:
        raise SystemExit(f"Need '{TIME}' to derive _hour")

    result = find_small_classes(df, args.qi, args.k_max)
    n_rows = int(result["k"].sum()) if not result.empty else 0
    n_classes = len(result)

    header = {
        "csv": args.csv,
        "qi": args.qi,
        "k_max": args.k_max,
        "n_classes": n_classes,
        "n_rows": n_rows,
        "total_rows": len(df),
    }

    if args.fix:
        if args.output:
            chosen = Path(args.output)
            out_path = str(chosen if chosen.parent != Path(".") else Path("outputs") / chosen.name)
        else:
            out_path = str(Path("outputs") / Path(args.csv).name)
        action = ask_fix_action()
        header["fix_action"] = action
        if action == "drop":
            kept = drop_small_classes(df, args.qi, args.k_max)
            write_release(kept, out_path)
            header["written"] = out_path
            header["rows_kept"] = len(kept)
            header["rows_dropped"] = n_rows
            print(f"Dropped {n_rows} rows in {n_classes} small classes; wrote {len(kept)} rows to {out_path}")
        else:
            column = ask_anonymize_column(df)
            header["anonymize_column"] = column
            updated = anonymize_small_classes(df, args.qi, args.k_max, column)
            write_release(updated, out_path)
            header["written"] = out_path
            header["rows_restricted"] = n_rows
            print(
                f"Set {column} to RESTRICTED on {n_rows} rows in {n_classes} small classes; "
                f"wrote {len(updated)} rows to {out_path}"
            )

    if args.json:
        records = []
        for rec in result.to_dict(orient="records"):
            rec["_hour"] = rec["_hour"].isoformat() if hasattr(rec.get("_hour"), "isoformat") else rec.get("_hour")
            records.append(rec)
        print(json.dumps({**header, "classes": records}, default=str, indent=2))
        return

    print(
        f"QI={args.qi}  k<={args.k_max}  classes={n_classes}  "
        f"rows={n_rows}/{len(df)}"
    )
    if result.empty:
        print("No equivalence classes at or below k_max.")
        return

    show = result.copy()
    if "_hour" in show.columns:
        show["_hour"] = show["_hour"].astype(str)
    pd.set_option("display.max_colwidth", 120)
    pd.set_option("display.max_rows", None)
    print(show.to_string(index=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
