#!/usr/bin/env python3
"""
Create scatter plots for every pair of selected CSV columns.

Usage:
    python scatter_plots.py input.csv --columns col_a col_b col_c
    python scatter_plots.py input.csv --columns col_a col_b --output-dir outputs
"""

import argparse
import itertools
import re
from pathlib import Path
from typing import List, Optional

import matplotlib.pyplot as plt
import pandas as pd


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", str(name).strip())
    return cleaned.strip("._") or "column"


def scatter_pairs(csv_path: str, columns: List[str], output_dir: str, sample: Optional[int]) -> None:
    df = pd.read_csv(csv_path)
    missing = [col for col in columns if col not in df.columns]
    if missing:
        available = ", ".join(df.columns.astype(str))
        raise SystemExit(f"Unknown column(s): {', '.join(missing)}\nAvailable: {available}")

    if len(columns) < 2:
        raise SystemExit("Need at least two columns to create scatter plots.")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    work = df[columns].apply(pd.to_numeric, errors="coerce")
    if sample is not None and len(work) > sample:
        work = work.sample(n=sample, random_state=0)

    pairs = list(itertools.combinations(columns, 2))
    for x_col, y_col in pairs:
        pair = work[[x_col, y_col]].dropna()
        filename = f"{sanitize_filename(x_col)}_{sanitize_filename(y_col)}.png"
        path = out / filename

        fig, ax = plt.subplots(figsize=(8, 6))
        ax.scatter(pair[x_col], pair[y_col], alpha=0.4, s=12, edgecolors="none")
        ax.set_xlabel(x_col)
        ax.set_ylabel(y_col)
        ax.set_title(f"{x_col} vs {y_col}")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        print(f"Saved {path} ({len(pair)} points)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a scatter plot for each pair of selected CSV columns."
    )
    parser.add_argument("csv", help="Path to the input CSV file")
    parser.add_argument(
        "--columns",
        "-c",
        nargs="+",
        required=True,
        help="Column names from the CSV to plot pairwise",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default="outputs",
        help="Directory where PNG files are written (default: outputs)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Optional max number of rows to sample before plotting",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scatter_pairs(args.csv, args.columns, args.output_dir, args.sample)


if __name__ == "__main__":
    main()
