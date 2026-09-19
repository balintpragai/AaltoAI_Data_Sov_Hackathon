# AaltoAI Data Sovereignty Hackathon

Privacy test suite for a mobile-network dataset release. Run `tests.py` on the **anonymised release candidate** (for example `out.csv`), not on the raw file.

Requires **Python 3.9+**, **pandas**, **numpy**, and **matplotlib** (for scatter plots).

```bash
pip install -r requirements.txt
```

---

## What `tests.py` does

The script loads a CSV, derives helper time columns, and runs nine automatable privacy tests. Each test prints one `PASS` / `WARN` / `FAIL` / `SKIP` / `INFO` / `ERROR` line. The worst status becomes the overall result.

It also writes a JSON audit report (default `privacy_report.json`) with:

- dataset SHA-256
- row count and column names
- parameters and thresholds used
- full per-test details

Exit code is **2** if the overall result is `FAIL` or `ERROR`, otherwise **0**.

Thresholds are engineering heuristics, not legal limits. A `PASS` means these specific attacks did not succeed on this file. It is **not** proof of anonymity under GDPR Recital 26.

---



## How to use it with `out.csv`

`out.csv` is the typical release-candidate file (for example the output of `data_transformator.py`).

```bash
# From the repo root
python tests.py out.csv
```

That:

1. Reads `out.csv`
2. Auto-detects a subscriber column (`msisdn`, otherwise `imsi`, otherwise none)
3. Prints the nine test lines
4. Writes `privacy_report.json`

Useful variants:

```bash
# Custom report path
python tests.py out.csv --out out_privacy_report.json

# Helsinki hour-of-day for the home-cell test (T5)
python tests.py out.csv --tz Europe/Helsinki

# If identifiers were dropped, name a remaining token column
python tests.py out.csv --user-col imei

# Stronger T9: a control CSV of people who are NOT in the release
python tests.py out.csv --holdout ctrl.csv
```

If `out.csv` has no `msisdn` / `imsi` (common after PII removal), T2, T4, T5, and T6 skip, and T3 counts **rows** as the anonymity unit instead of distinct subscribers. That is expected; it is not a crash.

### Demo (no CSV required)

```bash
python tests.py --demo raw        # weak synthetic data; should FAIL several tests
python tests.py --demo hardened   # mitigated synthetic data
```

`--demo raw` also writes `demo_raw.csv` and tries a small MSISDN hash brute-force (`--msisdn-prefix +358401`).

---

## How to use `scatter_plots.py`

`scatter_plots.py` reads a CSV and a list of column names, then writes one scatter plot for every unique pair of those columns. Each file is named `column1_column2.png`.

```bash
# From the repo root
python scatter_plots.py out.csv --columns data_GB_sum tp_dl_avg tp_ul_avg
```

That:

1. Reads `out.csv`
2. Plots every pair among the given columns (`data_GB_sum` vs `tp_dl_avg`, `data_GB_sum` vs `tp_ul_avg`, `tp_dl_avg` vs `tp_ul_avg`)
3. Saves PNGs in the current directory

Useful variants:

```bash
# Write plots into a folder
python scatter_plots.py out.csv --columns data_GB_sum tp_dl_avg tp_ul_avg --output-dir plots

# Sample rows first on large files
python scatter_plots.py out.csv --columns data_GB_sum tp_dl_avg --sample 10000
```

Non-numeric values are treated as missing and dropped for that pair. Unknown column names exit with the list of columns in the file.

---

## Expected columns

The suite is built for this schema (`Dataset description.txt`):


| Column                                                                          | Role in tests                               |
| ------------------------------------------------------------------------------- | ------------------------------------------- |
| `time_start`                                                                    | Hour bucket, hour-of-day, split for re-link |
| `msisdn`, `imsi`, `imei`                                                        | Identifiers / subscriber token              |
| `enb`, `province`, `radio_access_type`                                          | Quasi-identifiers                           |
| `application_category`                                                          | Sensitive inference (T8, T9)                |
| `data_GB_sum`, `im_video_GB_sum`, `im_audio_GB_sum`, `tethering_data_GB_dl_sum` | Volume fingerprints                         |
| Throughput / RTT / HTTP metric columns                                          | Re-link fingerprints (T6, T7)               |


Missing columns cause the tests that need them to `SKIP`, not fail the whole run.

`time_start` may be ISO text or a Unix timestamp (seconds or milliseconds). Numeric values whose median is above `1e11` are treated as milliseconds.

---



## The nine tests



### T1 — Identifier scan

Samples non-float columns (except `time_start`) and looks for IMEI-like (15 digits + Luhn), IMSI-like, and E.164 phone-like strings.

- `FAIL` if any pattern hits ≥ 1% of sampled values in a column
- `WARN` if hits exist but stay below 1%
- Cell IDs that happen to be 15 digits can be false positives



### T2 — Pseudonym brute-force

Attacks the subscriber column (`msisdn` or `imsi`):

- Raw phone/IMSI-shaped values → `FAIL`
- Opaque non-hex tokens → `PASS` (mapping-table leakage is out of scope)
- Hex digests (MD5/SHA) without `--msisdn-prefix` → `WARN`
- With `--msisdn-prefix` / `--msisdn-digits`, enumerates candidates and tries to reverse unkeyed hashes



### T3 — k-anonymity / risk

Groups records by quasi-identifiers (hour + cell, all base QIs, then QIs plus application and volumes). `k` is the number of distinct subscribers in the class, or the number of rows if there is no subscriber column.

Default: `k_min=10`. Any singleton (`k=1`) is `FAIL`. A large share of classes below `k_min` is `WARN`.

### T4 — Trajectory unicity

de Montjoye-style: given `p` random known `(hour, cell)` points, how often is the user unique? Evaluated at `p = 2, 3, 4`. Verdict uses `p=4` (fail if share > 0.05, warn if > 0.01). Needs subscriber, time, and `enb`.

### T5 — Home-cell exposure

Night hours (22:00–06:00 in `--tz`) modal cell as a home proxy. `FAIL` if more than 5% of users share that cell with fewer than `k_min` users. Skips if night resolution was already removed from `time_start`.

### T6 — Metric fingerprint re-link

Splits the timeline in half, builds per-user mean metric vectors, and measures how often the nearest neighbour in the other half is the same user (optionally plus modal cell). `FAIL` if top-1 rate > 5% **and** > 10× random; `WARN` at > 1% and > 3×.

### T7 — Value precision and outliers

Per numeric volume/metric column: uniqueness of exact values, decimal places, and extreme max vs p99.9. High uniqueness on volumes is `FAIL`; too many decimals or outliers are `WARN`.

### T8 — l-diversity / t-closeness

Within QI classes, checks whether `application_category` is almost homogeneous (>90% one category) or far from the global mix (total variation distance > `--t-close`, default 0.3). Also flags category names that look like GDPR Art. 9 special-category hints (`vpn`, `dating`, `health`, …).

### T9 — Inference vs control

Attacker knows the target’s QIs and guesses the class-majority `application_category` from the release. Compares accuracy on members vs a control set:

- `--holdout` file (best)
- otherwise 70/30 split by subscriber
- otherwise 70/30 split by row

Risk is `(member_acc − control_acc) / (1 − control_acc)`. Warn ≥ 0.05, fail ≥ 0.2.

---



## Command-line options


| Flag                                  | Default               | Meaning                                           |
| ------------------------------------- | --------------------- | ------------------------------------------------- |
| `csv`                                 | —                     | Release-candidate path (required unless `--demo`) |
| `--holdout`                           | —                     | Control CSV of non-members                        |
| `--user-col`                          | `msisdn` then `imsi`  | Subscriber / token column                         |
| `--out`                               | `privacy_report.json` | JSON report path                                  |
| `--demo`                              | —                     | `raw` or `hardened` synthetic data                |
| `--tz`                                | `UTC`                 | Time zone for hour-of-day                         |
| `--seed`                              | `0`                   | RNG seed                                          |
| `--k-min`                             | `10`                  | k-anonymity target                                |
| `--max-share-below-k`                 | `0.01`                | T3 warn threshold                                 |
| `--unicity-warn` / `--unicity-fail`   | `0.01` / `0.05`       | T4 thresholds                                     |
| `--t-close`                           | `0.3`                 | T8 t-closeness                                    |
| `--n-samples`                         | `500`                 | Unicity samples per `p`                           |
| `--n-attacks`                         | `1000`                | T9 attacks per group                              |
| `--max-users`                         | `2000`                | T6 subscriber cap                                 |
| `--scan-rows`                         | `100000`              | T1 sample size                                    |
| `--mcc-mnc`                           | empty                 | Tighten IMSI prefix in T1                         |
| `--msisdn-prefix` / `--msisdn-digits` | empty / `9`           | Enable T2 hash enumeration                        |
| `--max-candidates`                    | `3000000`             | T2 search budget                                  |
| `--n-targets`                         | `200`                 | T2 hashes to recover                              |


---



## How the script is structured

1. **Schema constants** — identifier, volume, metric, and QI column names.
2. **Helpers** — Luhn (IMEI), timestamp prep (`_ts`, `_hour`, `_hod`), k per equivalence class, modal cell.
3. **Tests T1–T9** — each returns `{test, status, summary, details}`.
4. `make_demo` — synthetic “raw” vs “hardened” data for a dry run.
5. `main` — argparse, load CSV (IDs as strings), run tests, print table, write JSON, set exit code.

A test exception becomes `ERROR` for that test; the rest of the suite still runs.

---



## Reading the result

Console:

```
File: out.csv | rows: 12,345 | subscriber column: None
------------------------------------------------------------------------------
[PASS ] T1 identifier scan                ...
[SKIP ] T2 pseudonym brute-force          No subscriber column in file: ...
...
------------------------------------------------------------------------------
OVERALL: WARN  (heuristic thresholds; see excluded tests before any claim of anonymity)
Full report: privacy_report.json
```

Open `privacy_report.json` for per-QI k values, unicity rates, re-link top-1/top-5, and inference risk. Do not treat `PASS` as a legal sign-off.