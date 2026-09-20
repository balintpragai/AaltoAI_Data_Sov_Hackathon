# AaltoAI Data Sovereignty Hackathon

Privacy test suite for a mobile-network dataset release. Run `tests.py` on the **anonymised release candidate** (for example `out.csv`), not on the raw file.

Requires **Python 3.9+**, **pandas**, **numpy**, and **matplotlib** (for scatter plots).

```bash
pip install -r requirements.txt
```

---

## What `tests.py` does

The script loads a CSV, derives helper time columns, and runs nine automatable privacy tests. Each test prints one `PASS` / `WARN` / `FAIL` / `SKIP` / `INFO` / `ERROR` line. The worst status becomes the overall result.

It also writes a JSON audit report (default `outputs/privacy_report.json`) with:

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
4. Writes `outputs/privacy_report.json`

Useful variants:

```bash
# Custom report path
python tests.py out.csv --out outputs/out_privacy_report.json

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

`--demo raw` also writes `outputs/demo_raw.csv` and tries a small MSISDN hash brute-force (`--msisdn-prefix +358401`).

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
3. Saves PNGs in `outputs/`

Useful variants:

```bash
# Write plots into a folder
python scatter_plots.py out.csv --columns data_GB_sum tp_dl_avg tp_ul_avg --output-dir outputs

# Sample rows first on large files
python scatter_plots.py out.csv --columns data_GB_sum tp_dl_avg --sample 10000
```

Non-numeric values are treated as missing and dropped for that pair. Unknown column names exit with the list of columns in the file.

---

## How to use `guide_anonymize.py`

Transforms a raw schema CSV into a release candidate (drop `msisdn`/`imsi`, TAC-only `imei`, hour-bucket `time_start`, round metrics, then k-anonymity). Full column-by-column notes: [`docs/guide_anonymize.md`](docs/guide_anonymize.md).

```bash
python guide_anonymize.py input.csv kanon.csv --k-min 10 --sig-figs 3
```

Writes `outputs/kanon.csv` and a JSON report (default `outputs/kanon_report.json`). Then run `python tests.py outputs/kanon.csv`.

---

## How to use `redacted_profile.py`

Builds a **redacted column profile** from a CSV. This is the only stats object allowed to leave the processing environment. Do **not** send `column_stats.py` output to a model (it stores `top_value` and identifier plots).

The profile keeps counts, missingness, uniqueness, and coarse numeric quantiles. It strips identifier values, cell IDs (`enb` / `enb_id`), and non-allowlisted category labels. Allowlisted labels (`province`, `radio_access_type`, `application_category`) are k-suppressed: rare values are omitted or rolled into `OTHER`.

Humans run this on real CSVs. Automated tests use synthetic frames only. Do not commit the JSON; keep it under `outputs/` (gitignored).

```bash
python redacted_profile.py input.csv --subject raw -o outputs/profile_raw.json
python redacted_profile.py release.csv --subject release_candidate --k-min 10
```

That:

1. Reads the CSV
2. Redacts each column by kind (identifier / enb / allowlisted categorical / numeric / other)
3. Writes JSON with `subject`, `redaction.stripped`, per-column stats, and `profile_sha256`

If `-o` is omitted, the default path is `outputs/profile_<subject>.json`.


| Flag         | Default | Meaning                                                                 |
| ------------ | ------- | ----------------------------------------------------------------------- |
| `csv`        | —       | Input CSV (required)                                                    |
| `--subject`  | —       | `raw` or `release_candidate` (required)                                 |
| `--k-min`    | `10`    | Minimum count before an allowlisted category label is kept              |
| `--sig-figs` | `3`     | Significant figures for numeric min/max and percentiles                 |
| `-o` / `--output` | `outputs/profile_<subject>.json` | JSON output path |


---

## How to use `privacy_review.py`

Advisory review of a **redacted profile** only — never rows, never `column_stats.py` output. It does not run inside `guide_anonymize.py` and does not write pipeline column lists. Decision: [`docs/adr/0002-privacy-review-not-in-the-transform.md`](docs/adr/0002-privacy-review-not-in-the-transform.md).

Point `PRIVACY_REVIEW_BASE_URL` at an **EU/on-prem** OpenAI-compatible host (not `api.openai.com`). The review JSON stays under `outputs/` until a human checks it for leaked labels. Promoting markdown into git is a human step.

Typical flow:

```bash
export PRIVACY_REVIEW_BASE_URL=https://your-eu-host.example/v1
export PRIVACY_REVIEW_MODEL=your-model
# export PRIVACY_REVIEW_API_KEY=...   # optional Bearer token; never commit

python privacy_review.py request outputs/profile_raw.json -o outputs/review_raw.json
python privacy_review.py render outputs/review_raw.json
python privacy_review.py sign outputs/review_raw.json --suggestion-id col:enb \
  --disposition accepted --by ada --role data_owner
python privacy_review.py must-answer outputs/review_release.json --question-id q1 --on --by ada --role data_owner
python privacy_review.py answer outputs/review_release.json --question-id q1 --text "..." --by ada --role data_owner
python privacy_review.py share-check outputs/review_release.json
python privacy_review.py promote outputs/review_release.json --markdown docs/privacy-reviews/review.md
```

`request` also writes a sibling `.md` next to the review JSON. Call logs append to `outputs/privacy_review_calls.jsonl`.

Environment (for `request` unless `--offline-json`):


| Variable                    | Meaning                                                                 |
| --------------------------- | ----------------------------------------------------------------------- |
| `PRIVACY_REVIEW_BASE_URL`   | OpenAI-compatible base URL (`…/v1` or full `…/chat/completions`)         |
| `PRIVACY_REVIEW_MODEL`      | Model name                                                              |
| `PRIVACY_REVIEW_API_KEY`    | Optional Bearer token                                                   |
| `PRIVACY_REVIEW_TIMEOUT`    | HTTP timeout in seconds (default `60`)                                  |


Subcommands:


| Command        | What it does |
| -------------- | ------------ |
| `request`      | Call the model (or `--offline-json`) with a redacted profile; write review JSON + markdown. Refuses profiles that look like `column_stats` (`top_value`). |
| `render`       | Print the review as markdown, or write it with `-o`. |
| `sign`         | Set a column suggestion disposition (`accepted` / `rejected` / `deferred`). Accepting a direct identifier or quasi-identifier requires `--role data_owner`. |
| `must-answer`  | Human flags an open question as blocking (`--on`) or not (`--no-on`). |
| `answer`       | Record the answer text for a question. |
| `share-check`  | Whether a **release_candidate** review has no unanswered must-answer questions. Exit `0` if so; `1` if a must-answer is still open; `2` if the subject is not `release_candidate` (raw is never shareable). Still not legally anonymous. |
| `promote`      | Copy rendered markdown to a path you may commit after a human leak check. Do not copy the JSON profile into git. |


| Flag / argument | Used by | Meaning |
| --------------- | ------- | ------- |
| `profile` | `request` | Path to redacted profile JSON |
| `review` | all others | Path to review JSON |
| `-o` / `--output` | `request`, `render` | Review JSON path (`request`; default `outputs/review_<subject>.json`) or markdown path (`render`; default stdout) |
| `--offline-json` | `request` | Skip HTTP; parse this file as model JSON (tests / air-gap) |
| `--suggestion-id` | `sign` | Column suggestion id, e.g. `col:enb` |
| `--disposition` | `sign` | `accepted`, `rejected`, or `deferred` |
| `--question-id` | `must-answer`, `answer` | Open question id, e.g. `q1` |
| `--on` / `--no-on` | `must-answer` | Set `must_answer` true (default) or false |
| `--text` | `answer` | Answer body |
| `--by` | `sign`, `must-answer`, `answer` | Who recorded the action |
| `--role` | `sign`, `must-answer`, `answer` | `engineer` or `data_owner` |
| `--markdown` | `promote` | Destination markdown path |

Synthetic tests: `python test_privacy_review.py`.

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
| `--out`                               | `outputs/privacy_report.json` | JSON report path                                  |
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
Full report: outputs/privacy_report.json
```

Open `outputs/privacy_report.json` for per-QI k values, unicity rates, re-link top-1/top-5, and inference risk. Do not treat `PASS` as a legal sign-off.