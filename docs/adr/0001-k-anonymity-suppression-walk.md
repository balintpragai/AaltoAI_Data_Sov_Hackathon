# Two-pass lowest-utility attribute suppression, then record suppression

k-anonymity used to drop every row whose quasi-identifier combination was smaller than `k_min`. We instead attribute-suppress `application_category` (`RESTRICTED`) then, if still needed, `enb` (`0`, to keep the integer column), and drop only classes that remain small. That keeps more rows than immediate record suppression, at the cost of coarser values on the two quasi-identifiers we can least afford to keep exact for analysis.

**Considered options:** drop small classes immediately (simpler, more data loss); walk every quasi-identifier including `time_start` (too much utility loss, and `RESTRICTED` does not belong in a numeric hour bucket).
