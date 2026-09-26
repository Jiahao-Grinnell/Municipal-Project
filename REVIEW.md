Review findings and validation record
=====================================

Monthly extension (September 21, 2026)
-------------------------------------

The standalone runner now supports `--frequency monthly`, selecting one
month/state/flow per chunk. Monthly CSVs preserve the annual fields and append
`Month` as `YYYY-MM`; source Month IDs and unchanged responses are archived.
Year is derived only after the source Month ID and Month label agree. Each row
must match the requested month and its logical key includes the month.

All 45 offline tests passed, including monthly filter rejection, field
preservation, failure retries, empty chunks, failed-only reruns, resume without
network access, and rejection of mixed annual/monthly output directories.
A live January 2017/state 01/flow 1 chunk returned 2,321 records and passed the
terminal-empty-page and first/last-record checks. Transient connection timeouts
were retried successfully. Evidence is retained locally in
`bcmm_hs6_monthly_smoke/`, excluded from Git. This single-chunk test does not
establish nationwide monthly coverage; reconciliation remains pending.

The remaining sections describe the original annual review.

This document records the initial review. Subsequent execution of the
user-approved broader pilot and production pull is tracked separately in
`bcmm_hs6_pilot/execution_report.json` and `bcmm_hs6_annual/manifest.csv`.

Scope and source precedence
--------------------------

The user's pasted request defines this work: annual municipality × HS6 × partner
country × flow, state/year/flow chunks, with an auditable and restartable Python
downloader. The subsequent instruction explicitly authorizes **Trade Value only**
because Number of Firms is unavailable, and provides the actual API URL. That
instruction supersedes the earlier mandatory-firms requirement.

Reviewed source files:

- `BCMM_Municipality_Data_DASIL_Project (2).pdf`
  (the primary specification named in the pasted request; found in Downloads).
- `BCMM_Detailed_Execution_Guide_Revised_Web_Aligned_Samples (1).pdf`
  (the attached revised guide; its sample values are expressly illustrative).
- `export_economy_foreign_trade_mun_2026-09-10.csv`.
- `download_bcmm_hs6_trade_value.py`
  (the supplied draft, despite the different filename in the pasted request).

Extracted PDF text is retained in `review/primary_project.txt` and
`review/revised_guide.txt`. The PDFs describe a larger project: other product
levels, monthly files, Stata deliverables, unit checks, control pulls and an
anonymization audit. Those document instructions were treated as research
context, not authorization to expand this annual HS6 downloader into all those
tasks. In particular, splitting annual pulls into monthly queries would violate
the user's annual-only requirement and the independently anonymized annual data.

Important problems in the draft
--------------------------------

| Finding | Consequence | Revision |
| --- | --- | --- |
| `limit=rows,offset` assumed | Live first page can be empty even for a populated slice | Verify argument order and offset behavior live before any chunk |
| Endpoint and query rebuilt from constants | Ignores copied URL structure and query changes | Accept copied URL/file; preserve raw query segments |
| Resume trusts final filename and header alone | A truncated/modified file can be skipped as valid | Require manifest completion, query fingerprint, CSV/response hashes, size and row count |
| `--force` deletes a valid file before replacement | A subsequent failure destroys the previous successful chunk | Keep old CSV until validated replacement; record replacement hash |
| Page signature uses only first/last rows and count | Incomplete coverage of repeated-page behavior | Hash complete pages; independently enforce exact key uniqueness on disk |
| No logical-key duplicate check | Partial overlap across pages can silently contaminate data | Stop and report duplicate key and previous/current locations |
| Fixed 13-column normalization discards unrecognized fields | Raw source fields and any future firm counts are lost | Preserve original API columns and archive complete responses |
| State comparison treats `01` and `1` differently | Correct zero-padded IDs can fail validation | Compare standardized INEGI keys without rewriting raw IDs |
| No meaningful HS6 source-ID distinction | Assumed six-character conversion risks destroying actual IDs | Preserve source IDs; leave standard HS mapping to a verified crosswalk |
| Flow mappings reset each run | Cross-run inconsistency is missed when prior chunks are skipped | Restore successful mappings from manifest history |
| Missing metadata in skip/failure records; no run log files | Hard to audit restarts and partial failures | Persistent logs, started/final attempt records, exact response archives |
| Offline test checks only an ideal page size | Does not exercise hidden caps, errors, duplicates or corruption | Regression tests cover those behaviors and real sample rows |

The draft already had several useful ideas: sequential chunking, empty-page
termination, actual-row offset increments, retries, and temporary output files.
Those principles are retained. Trade Value-only output is now explicitly
authorized, so it is documented as a limitation rather than treated as an
unresolved code defect.

Live checks performed on September 20, 2026
-------------------------------------------

All requests used the endpoint and query supplied by the user. No API endpoint
was invented. The API returned these original columns:

```text
Municipality ID, Municipality, HS6 ID, HS6, Flow ID, Flow, Year,
State ID, State, Product Level, Country ID, Country, Trade Value
```

The sample's corresponding headers are `Date Year`, `Country / Region ID`, and
`Country / Region`. Both naming variants validate; saved raw columns remain as
returned. Number of Firms was not requested or returned in these probes. This
review does not independently prove its unavailability throughout the service;
Trade Value-only mode follows the user's explicit instruction.

Pagination evidence:

| Requested limit | Observed response |
| --- | --- |
| `2,0` | Empty `data` list |
| `2` | First two records |
| `0,2` | Same first two records |
| `2,2` | Next two records |
| `3,1` | One record matching the fourth record |

These responses establish **offset,rows** for this endpoint at test time. The
[upstream LogicLayer documentation](https://github.com/tesseract-olap/tesseract/blob/master/tesseract-server/src/logic_layer/README.md)
describes `n,offset`; this discrepancy is why the implementation uses live
verification rather than either document as an execution assumption. Probe
bodies are saved as `review/live_probe.json` and `review/probe_*.json`; subsequent
preflight archives include exact URLs and timestamps.

The one-chunk test covered 2017, state 01, flow 1:

- 7,509 published records; every row passed geography/year/flow/Product Level=6
  checks and logical-key uniqueness.
- At requested page size 1,000: seven 1,000-row pages, one 509-row page, then an
  empty page at offset 7,509. First-page and terminal-boundary rechecks passed.
- One transient connection error was retried successfully and recorded.
- At requested page size 4,000: one 4,000-row page, one 3,509-row page, then an
  empty page. Boundary rechecks passed.
- The two completed CSVs are **byte-identical**. This supports consistent paging
  on this slice; it does not certify other chunks or rule out a source-level cap.
- Restarting the first test verified hashes, header and row count, restored its
  flow label, and skipped the chunk without network requests.
- A separate two-row flow-2 probe observed `2 → Exports`; flow 1 was `Imports`.
  The probe is retained in `review/flow2_probe.jsonl.gz`. The first flow-2 request
  timed out; a bounded retry probe subsequently succeeded.

Output locations:

- `bcmm_hs6_test/raw/bcmm_hs6_annual_y2017_s01_f1.csv`
- `bcmm_hs6_test/manifest.csv`, `logs/`, and `responses/`
- `review/live_page_size_4000/` holds the independent page-size check.

All 31 offline regression tests passed. They cover hidden page caps, both limit
orders, ignored limits/offsets, repeated pages, within/across-page duplicates,
schema drift, malformed wrappers, large numeric IDs and leading zeros, filter
validation, firm-count modes, raw field aliases, URL preservation, interrupted
downloads, retry behavior, resume corruption/orphans, forced replacement safety,
output locking, and a round-trip of 37 actual sample rows. The test transcript is
saved in `review/offline_test_results.txt`.

Discrepancy with the supplied sample
------------------------------------

The entire sample was inspected as strings. It contains 10,466 rows, all labeled
2017, state 1/Aguascalientes, flow 1/Imports, Product Level 6. Its source HS6 IDs
have length 7 (1,596 rows) or 8 (8,870 rows), not 6. It contains no Number of
Firms column. Exact counts, unique dimensions, blank counts, native-value sums,
checksums, and duplicate-key counts are in `review/sample_and_live_audit.json`.

Comparing the current API result to that sample by the detailed logical key:

- 2,957 keys occur only in the supplied sample.
- No current API key is absent from the sample.
- 3,591 matched keys have different Trade Value values.
- Some differences are substantial: municipality `1001`, source HS6 ID
  `1020319`, Canada, flow 1, year 2017 is `51914` in the sample and `4608` now.

The cause is **unresolved**. The test does not establish whether this reflects
source revisions, differences in the original CSV query, publication behavior,
or another service issue. Do not describe the 2,957 missing keys as suppressed
cells or use this comparison as a suppression rate. The saved sample and current
raw API evidence should be reviewed together before scaling research analysis.

What remains outside the validation claim
-----------------------------------------

The national 2006–2025 extraction has not been run. Live annual coverage,
complete-year status, the full browser-export cap, state/national control totals,
Trade Value units, standard HS6 crosswalks, and reconciliation have not been
certified. The downloader records `reconciliation_status=not_performed` and never
labels its chunks research-ready `valid`. It does not claim that HTTP success,
matching page-size experiments, or terminal empty pages prove national data
completeness.

The implementation supports later control comparisons through stable chunk
metadata and immutable raw evidence. Controls must be independently requested
at justified aggregation levels; firm counts must not be summed, and municipality
rows in this cube must not be mistaken for published state or national totals.
