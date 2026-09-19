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
