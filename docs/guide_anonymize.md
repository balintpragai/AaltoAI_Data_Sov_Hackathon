# What `guide_anonymize.py` does to a CSV

The script reads a mobile-network CSV, writes an anonymised CSV under `outputs/`, and writes a JSON report of what changed. It does **not** prove legal anonymity (GDPR Recital 26); it applies a fixed transform pipeline aimed at the EDPB three-criteria framing (no record isolation, no linkage, no inference).

```bash
python guide_anonymize.py input.csv output.csv
python guide_anonymize.py input.csv output.csv --k-min 10 --sig-figs 3
python guide_anonymize.py input.csv output.csv --report outputs/out_guide_report.json
```

The second argument is a **filename**. The file is always written as `outputs/<that name>`. Default `k_min` is 10; default significant figures is 3.

---

## Column-by-column

| Step | Columns | What happens |
| --- | --- | --- |
| 1. Drop | `msisdn`, `imsi` | Removed if present. Direct identifiers (GDPR Art. 4(1)). |
| 2. Generalise | `imei` | Kept as the first 8 digits only (TAC: device model/manufacturer, not one handset). Empty/missing stays missing. |
| 3. Coarsen time | `time_start` | Unix seconds → `floor(ts / 3600)` (hours since epoch). |
| 4. Round numbers | Volume and KPI columns listed below | Each value rounded to `--sig-figs` **significant figures** (not decimal places). |
| 5. k-anonymity | Quasi-identifiers: `time_start`, `province`, `radio_access_type`, `enb` or `enb_id`, `application_category` | Small groups are coarsened, then leftover small groups are dropped. |

**Rounded columns** (if present): `data_GB_sum`, `im_video_GB_sum`, `im_audio_GB_sum`, `tethering_data_GB_dl_sum`, `tp_dl_avg`, `tp_ul_avg`, `tp_dl_filtered_avg`, `cont_rtt_radio_avg`, `cont_rtt_internet_avg`, `initial_rtt_radio_avg`, `tcp_retrans_byte_ratio_downlink_avg`, `tcp_retrans_byte_ratio_uplink_avg`, `http_response_time_avg`, `http_sr_avg`.

**Unchanged** (except when they take part in k-anonymity): `province`, `radio_access_type`, and other columns not listed above.

---

## k-anonymity (step 5)

An equivalence class is every unique combination of the QI columns above.

1. If a class has fewer than `k_min` rows, set `application_category` to `RESTRICTED` on those rows, then recompute class sizes.
2. If a class is still too small, set the cell column (`enb` / `enb_id`) to `0` on those rows, then recompute again. Numeric cell IDs cannot store the string `RESTRICTED`.
3. Any class still below `k_min` is **dropped** (those rows do not appear in the output).

`time_start` is never blanked in this walk. Details: `docs/adr/0001-k-anonymity-suppression-walk.md`.

---

## Output

- **CSV:** same remaining columns, fewer rows if record suppression ran, IMEI shortened, time in hours, metrics coarsened.
- **JSON report:** input/output row counts, identifiers dropped, TAC/time/rounding settings, suppression passes, and how many rows were dropped vs kept.

Treat the output as personal data until a documented assessment says otherwise (GDPR Art. 4(5): this is de-identification / k-anonymisation, not a claim that identification is reasonably unlikely).
