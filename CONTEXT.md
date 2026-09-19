# Dataset release anonymisation

Turning mobile-network event windows into a release candidate that is harder to single out, without pretending the result is legally anonymous.

## Language

**Release candidate**:
The dataset row-set intended for sharing after identifiers are dropped or coarsened and isolation controls have been applied.
_Avoid_: Anonymous data, anonymised dump, output file (when talking about the dataset itself)

**Quasi-identifier**:
An attribute used with others to form an equivalence class for k-anonymity on this release: hour-bucketed `time_start`, `province`, `radio_access_type`, `enb` when present, and `application_category`.
_Avoid_: Identifier, QI set (when you mean a single attribute)

**Equivalence class**:
The set of rows that share the same quasi-identifier values.
_Avoid_: Group, bucket, cluster (when you mean this k-anonymity class)

**Small class**:
An equivalence class whose size is below `k_min`.
_Avoid_: Unique row, outlier, low class data

**Attribute suppression**:
Replacing a quasi-identifier value on rows that currently sit in a small class: `RESTRICTED` when the column is not numeric, `0` when it is (e.g. `enb`).
_Avoid_: Anonymization fix, anonymise the column, mask

**Record suppression**:
Removing every row that still sits in a small class after attribute-suppression passes.
_Avoid_: Filter, drop all rows, remove uniqueness

**Suppression walk**:
The only quasi-identifiers eligible for attribute suppression, in utility order (lowest first): `application_category`, then `enb`. Hour-bucketed `time_start` is never suppressed this way.
_Avoid_: QI importance, anonymization fix order

**Column profile**:
Per-column statistics computed locally from a CSV (types, missingness, cardinality, numeric summaries, category counts).
_Avoid_: Schema, distribution (when you mean this object), metadata

**Redacted column profile**:
The only column profile allowed to leave the processing environment: names, types, optional descriptions, cardinality, null rates, coarsened numeric summaries, and k-suppressed category counts — never identifier-like values, never rare labels, never raw `enb` IDs.
_Avoid_: Schema sent to the model, anonymised stats, safe distribution

**Privacy review**:
A model-generated, non-binding set of suggestions on column sensitivity, recommended checks, and open questions. It is not a DPIA, not anonymisation, and not a transform of any CSV.
_Avoid_: AI anonymiser, consultant verdict, automated decision

**Suggestion**:
One item in a privacy review. It has no effect on `guide_anonymize.py`, `tests.py`, or a release candidate until a human accepts it.
_Avoid_: Finding, ruling, config, patch

**Check**:
A recommended verification on a column or release candidate that either names an existing `tests.py` id or is marked not implemented.
_Avoid_: Test (when you mean this suggestion), control, skill

**Open question**:
A gap a named human must answer; it is not a test result and not a pipeline parameter.
_Avoid_: TODO, risk, action item

**Must-answer open question**:
An open question a human has flagged so the file must not be treated as a shareable release candidate until that human clears it. The model may only propose the flag.
_Avoid_: Blocker, gate, failing test, automated hold

**Signed privacy review**:
The dated file that stores a privacy review plus each suggestion’s human disposition: accepted, rejected, or deferred.
_Avoid_: Privacy report (that is the `tests.py` JSON), consultant log, chat transcript

**Engineer**:
The person who may accept, reject, or defer suggestions about KPIs, rounding, or mapping checks to `tests.py`.
_Avoid_: Developer, consultant, reviewer (when you mean this role)

**Data owner**:
The designated person who may accept, reject, or defer suggestions that add or remove a direct identifier or quasi-identifier. That act is not a DPIA and not DPO sign-off by itself.
_Avoid_: DPO, admin, legal

**Policy pack**:
The fixed grounding included in every model call: this glossary, the AGENTS.md column table, WP216 singling-out / linkability / inference, EDPB isolation / linkage / inference, and that output is suggestions only.
_Avoid_: Skill, system prompt, consultant persona (when you mean this content)
