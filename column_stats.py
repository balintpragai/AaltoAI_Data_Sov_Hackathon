#!/usr/bin/env python3
"""
Per-column statistics for a CSV. String / name / id columns also get a
value-distribution bar plot.

Usage:
    python column_stats.py out.csv
    python column_stats.py out.csv --output-dir outputs
    python column_stats.py out.csv --top-n 25
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

ID_NAME_PATTERN = re.compile(
    r"(^|_)(id|imei|imsi|msisdn|enb|name|province|category|type|rat)(_|$)",
    re.IGNORECASE,
)


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", str(name).strip())
    return cleaned.strip("._") or "column"


def is_string_or_id(series: pd.Series, col: str) -> bool:
    if pd.api.types.is_string_dtype(series) or pd.api.types.is_object_dtype(series):
        return True
    if pd.api.types.is_categorical_dtype(series):
        return True
    return bool(ID_NAME_PATTERN.search(str(col)))


def column_stats(series: pd.Series) -> dict:
    n = len(series)
    missing = int(series.isna().sum())
    non_null = series.dropna()
    stats = {
        "dtype": str(series.dtype),
        "count": n,
        "missing": missing,
        "missing_share": round(missing / n, 6) if n else None,
        "n_unique": int(non_null.nunique()),
    }
    if non_null.empty:
        return stats

    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        desc = non_null.astype(float).describe(percentiles=[0.25, 0.5, 0.75, 0.99])
        stats.update(
            {
                "mean": float(desc["mean"]),
                "std": float(desc["std"]) if n > 1 else 0.0,
                "min": float(desc["min"]),
                "p25": float(desc["25%"]),
                "p50": float(desc["50%"]),
                "p75": float(desc["75%"]),
                "p99": float(desc["99%"]),
                "max": float(desc["max"]),
            }
        )
    else:
        vc = non_null.astype(str).value_counts()
        stats["top_value"] = str(vc.index[0])
        stats["top_count"] = int(vc.iloc[0])
        stats["top_share"] = round(int(vc.iloc[0]) / len(non_null), 6)
    return stats


def plot_distribution(series: pd.Series, col: str, path: Path, top_n: int) -> None:
    values = series.dropna().astype(str)
    if values.empty:
        return
    vc = values.value_counts()
    if len(vc) > top_n:
        head = vc.iloc[:top_n]
        other = int(vc.iloc[top_n:].sum())
        plot_s = pd.concat([head, pd.Series({"<other>": other})])
        title = f"{col} (top {top_n} + other, n={len(values)})"
    else:
        plot_s = vc
        title = f"{col} (n={len(values)}, {len(vc)} values)"

    height = max(4.0, 0.35 * len(plot_s) + 1.5)
    fig, ax = plt.subplots(figsize=(10, height))
    ax.barh(plot_s.index.astype(str)[::-1], plot_s.values[::-1], color="#4C78A8")
    ax.set_xlabel("count")
    ax.set_title(title)
    ax.grid(True, axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def run(csv_path: str, output_dir: str, top_n: int) -> None:
    df = pd.read_csv(csv_path)
    # df = df[df["province"] == 'Unavailable'].copy()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    report = {"csv": csv_path, "rows": len(df), "columns": {}}
    rows_for_table = []

    for col in df.columns:
        series = df[col]
        stats = column_stats(series)
        plot_this = is_string_or_id(series, col)
        stats["distribution_plot"] = None
        if plot_this:
            plot_path = out / f"dist_{sanitize_filename(col)}.png"
            plot_distribution(series, col, plot_path, top_n)
            stats["distribution_plot"] = str(plot_path)
            print(f"Saved {plot_path}")
        report["columns"][col] = stats
        rows_for_table.append({"column": col, **{k: v for k, v in stats.items() if k != "distribution_plot"}})

    table = pd.DataFrame(rows_for_table)
    table_path = out / "column_stats.csv"
    json_path = out / "column_stats.json"
    table.to_csv(table_path, index=False)
    json_path.write_text(json.dumps(report, indent=2, default=str))

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_colwidth", 40)
    print(table.to_string(index=False))
    print(f"Wrote {table_path}")
    print(f"Wrote {json_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print per-column statistics and plot distributions for string/name/id columns."
    )
    parser.add_argument("csv", help="Path to the input CSV")
    parser.add_argument(
        "--output-dir",
        "-o",
        default="outputs/column_stats",
        help="Directory for CSV/JSON summaries and PNG plots (default: outputs)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=50,
        help="Max distinct values shown on a distribution plot (rest grouped as <other>)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(args.csv, args.output_dir, args.top_n)


if __name__ == "__main__":
    main()
