Annual and monthly municipality HS6 / HS4 / HS2 BCMM downloader
=====================================

**Current standalone entry point:** `python run_download.py`. See
[Anaconda Prompt instructions](RUN_IN_ANACONDA.md). This entry point resumes
the existing output, uses generous timeouts and pacing, retries each failed chunk
up to 10 times, records unresolved failures, and continues the queue. The lower-level
commands and original pilot workflow below remain available for diagnostic use.

HS2 downloads
--------------

Keep the existing `base_api_url.txt` and use:

```bat
python run_download.py --product-level 2 --frequency annual --dry-run
python run_download.py --product-level 2 --frequency annual
python run_download.py --product-level 2 --frequency monthly --dry-run
python run_download.py --product-level 2 --frequency monthly
```

For 2006-2025, all 32 states and both flows, annual mode selects **1,280 chunks**
and writes to `bcmm_hs2_annual/`; monthly mode selects **15,360 chunks** and writes
to `bcmm_hs2_monthly/`. Product columns are `HS2 ID` and `HS2`. All other columns
follow the existing rules; monthly output appends `Month` as `YYYY-MM`.

The query selects the independently published **`HS2 2 Digit`** hierarchy with
**Product Level=2**, verified against the
[official cube metadata](https://www.economia.gob.mx/apidatamexico/tesseract/cubes).
Plain `HS2` is a different hierarchy and is rejected for this extraction. A copied
annual HS2 URL using `HS2 2 Digit` and Level=2 is also accepted. Source IDs stay
intact; no truncation or summation of HS4/HS6 is used to manufacture HS2 data.

Repeat a command to resume, or append `--retry-failed` to retry only its unresolved
chunks. Each level and frequency has its own manifest, archives, locks, and failure
ledger. The default remains HS6; HS4/HS6 request URLs, filenames, query fingerprints,
and resume compatibility are unchanged. An already running downloader keeps its
imported code in memory and does not need to be stopped or restarted for this update.

After the annual queue finishes with no unresolved failures, validate and merge it:

```bat
python combine_bcmm_chunks.py --product-level 2
```

This creates `bcmm_hs2_annual/bcmm_hs2_annual_2006_2025_all.csv` and an accompanying
`combined_manifest.json`. The merger is for annual data only.

Per-chunk API URLs
------------------

```bat
python export_chunk_api_urls.py
```

This writes `chunk_api_urls.csv` into each of the six `bcmm_hs{2,4,6}_{annual,monthly}`
folders, with one row per chunk: year, month, state, flow, status (`downloaded`,
`downloaded_empty` or `failed (...)`), row/page counts, and `api_url_first_page`.
The URLs come from the latest completed attempt in `manifest.csv`, or from
`failed_chunks.csv` for unresolved failures. They are the exact requests sent.
`limit=offset,rows` handles pagination: the first page is `limit=0,4000`, and a
chunk with `n_pages` > 1 continues with `limit=4000,4000`, `limit=8000,4000`, and
so on until an empty page is returned. The file is UTF-8 with a BOM so that Excel
opens it correctly. Rerun the command after any new download.

A folder with unresolved failures also gets `failed_chunk_api_urls.csv`, listing
only the failed chunks: year, month, state ID and name, flow, error (for example
`HTTP 502` or `ConnectTimeout`), last failure time, and API URL. When no failures
remain, the next run deletes it.

HS4 downloads
--------------

Use the same runner with `--product-level 4`. Annual and monthly data stay separate:

```bat
python run_download.py --product-level 4 --frequency annual --dry-run
python run_download.py --product-level 4 --frequency annual
python run_download.py --product-level 4 --frequency monthly --dry-run
python run_download.py --product-level 4 --frequency monthly
```

The default scope remains 2006?2025, states 01?32, both flows: **1,280 annual
chunks** (year/state/flow) or **15,360 monthly chunks** (month/state/flow).
Annual files go to `bcmm_hs4_annual/`; monthly files go to `bcmm_hs4_monthly/`.
Each directory has its own `raw/`, `responses/`, manifest, logs, failure ledger,
attempt history, and run summary. Repeating the same command resumes that queue.
Append `--retry-failed` to retry only unresolved failures for the selected mode.

Keep the existing annual HS6 `base_api_url.txt`. The adapter preserves its endpoint
and cuts, replacing the product drilldown with **`HS4 4 Digit`** and setting
**Product Level=4**. A copied annual HS4 URL using that same native hierarchy is
also accepted. The native hierarchy was checked against the
[official cube metadata](https://www.economia.gob.mx/apidatamexico/tesseract/cubes).
Plain `HS4` belongs to the six-digit Product hierarchy and must not be used for
this independent four-digit extraction. No HS6 values are summed to create HS4.

All other CSV columns retain their source names and values. Only the native
`HS4 4 Digit ID` and `HS4 4 Digit` headers become **`HS4 ID` and `HS4`**. Monthly
output uses the existing verified Year/Month adapter and appends `Month` as
`YYYY-MM`. Source product IDs remain intact strings; they are not truncated to
four characters. Response archives preserve the original headers and bodies.

The same page size, sequential pacing, ten complete attempts per chunk,
pagination checks, uniqueness checks, checksum validation, and exit codes apply.
Directories containing a different product level or frequency are rejected.
HS6 remains the default, with its existing filenames and resume fingerprints.
To validate all completed annual chunks and merge HS4 into one CSV, run:

```powershell
python combine_bcmm_chunks.py --product-level 4
```

The merger checks unresolved failures, pagination evidence, query fingerprints,
CSV and response archive hashes, columns, and row counts before merging. Output:
`bcmm_hs4_annual/bcmm_hs4_annual_2006_2025_all.csv`, with a
`combined_manifest.json` containing the row count and SHA256. The default remains
annual HS6; monthly files stay in their separate directory.

Single-chunk checks:

```bat
python run_download.py --product-level 4 --frequency annual --start-year 2017 --end-year 2017 --states 01 --flows 1
python run_download.py --product-level 4 --frequency monthly --start-year 2017 --end-year 2017 --months 01 --states 01 --flows 1
```

**Monthly mode:** run `python run_download.py --frequency monthly`. It selects
2006–2025, months 01–12, states 01–32, and both flows: **15,360 chunks**, each
covering one month, state, and flow. Results go to `bcmm_hs6_monthly/` by default.
Annual mode remains the default and uses `bcmm_hs6_annual/`.

Monthly CSVs retain the annual columns, including `Year` (or `Date Year` for a
template using that name), and append only `Month` in `YYYY-MM` format. The
monthly API returns `Month ID` and `Month`; the adapter verifies their agreement,
derives the year from the source month, and omits `Month ID` from the CSV.
Unmodified responses, including `Month ID`, remain in the response archives.
Logical keys include the month, and each row must match its requested month.
All other source fields and Trade Value strings are preserved.

The same annual `base_api_url.txt` is used as configuration. Monthly mode replaces
the annual drilldown with `Month` and the year cut with `Month=YYYYMM`; do not
replace that configuration file with a monthly URL. Startup pagination probes
use January of the template's reference year/state/flow. That reference slice
must be nonempty. Empty requested chunks are saved with explicit empty status;
they do not establish that a month or year is unavailable.

```bat
python run_download.py --frequency monthly --dry-run
python run_download.py --frequency monthly --start-year 2017 --end-year 2017 --months 01 --states 01 --flows 1
python run_download.py --frequency monthly
python run_download.py --frequency monthly --retry-failed
```

Completed chunks are validated and skipped. Monthly mode uses the same timeouts,
request pacing, ten-attempt chunk retries, and failure ledger as annual mode.
The month is explicit in chunk filenames and request URLs in the manifest and
failure ledger; their existing schemas are retained for annual compatibility.
Use distinct output directories for the two frequencies. The existing
`combine_bcmm_chunks.py` is an annual merger and must not be used for monthly
output. For monthly data, validate all 15,360 chunks and create four five-year CSVs:

```bat
python combine_bcmm_monthly.py --product-level 4
```

Files go to `bcmm_hs4_monthly/combined_5year/`, named
`bcmm_hs4_monthly_2006_2010.csv`, `2011_2015`, `2016_2020`, and `2021_2025`
(with the same prefix and `.csv` extension). All monthly rows and the `Month`
column are preserved; the data are concatenated without aggregation. A
`combined_manifest.json` records row counts and SHA256 for every output.
The merger checks source CSV/archive hashes, completion evidence, query
fingerprints, headers, row counts and period cuts. It refuses unresolved failures
or incomplete coverage and stages every group before committing final CSVs.

If some chunks still fail after retries (for example HS2 monthly, where 16 chunks hit
repeated HTTP 502/timeouts), you can merge the rest explicitly:

```bat
python combine_bcmm_monthly.py --product-level 2 --allow-missing
```

Only the chunks listed in `failed_chunks.csv` are excluded. Every other chunk must
still pass all checks, and a ledger entry for a completed chunk stops the merge.
The merger writes two lists to `combined_5year/`:

- `missing_chunks.csv` has one row per excluded chunk: year, month, `year_month`,
  state ID and name, flow ID and name, affected five-year file, and the error
  details and query URL from the failure ledger. Each chunk covers **all
  municipalities** of its state, so the whole state-month-flow is missing.
- `missing_chunks_municipalities.csv` has one row per excluded chunk and
  municipality: year, month, state, flow, and the municipality ID and name.
  The municipalities come from that state's *other downloaded chunks in the same
  year*. They show which municipalities are likely affected; this is not the
  source's own list for the missing month.

`combined_manifest.json` records `coverage="incomplete"`, `n_missing_chunks`,
the number missing from each output file, and both lists' SHA256 under
`missing_lists`. The merger always writes both files; when coverage is complete
they contain only the header. After a later
`--retry-failed` run recovers the chunks, rerun the merge without the flag.
Original chunks and downloader records are retained. Levels 2 and 6 are also
supported, with their separate source and output directories.

Download integrity checks do not establish full source coverage or
reconciliation; monthly and annual published values may differ, and monthly
values must not automatically replace the annual extraction.

Use `download_bcmm_hs6_annual.py`. It downloads published cells from
`economy_foreign_trade_mun`, with annual Year/Date Year, Municipality, HS6,
partner Country, Flow, State, and Product Level=6. It iterates year × state ×
flow, paging within each chunk. Defaults: 2006–2025, states 01–32, flows 1 and 2
(1,280 chunks). These years are the requested scope, not independently verified
complete-year coverage.

**Trade Value only is intentional**, following your subsequent instruction that
Number of Firms is currently unavailable. Its absence is logged and recorded.
No firm counts are invented. The original draft and input CSV in Downloads are
unchanged. The code requires Python 3.10+ and `requests`; it does not need pandas.

**Critical live finding:** your endpoint uses `limit=offset,rows`, contrary to the
ordering stated in the project text and used by the old script. A live request
with `limit=2,0` returned no records, whereas `limit=0,2` returned two. The revised
script verifies the order and offset movement on every run that needs downloads.
It does not infer completion from HTTP 200.

Install and run
--------------

From this project directory, with Python available in your terminal:

```powershell
python -m pip install -r requirements.txt
python -m unittest -v
python download_bcmm_hs6_annual.py --api-url-file base_api_url.txt --dry-run
```

If Python is not on the current PowerShell PATH, use Anaconda Prompt or invoke
the interpreter by its full installation path:

```powershell
& 'C:\path\to\anaconda3\python.exe' download_bcmm_hs6_annual.py --api-url-file base_api_url.txt --dry-run
```

`base_api_url.txt` contains **the exact URL you supplied**, not a guessed endpoint.
You can instead use `--api-url "COPIED_URL"`. Quote URLs containing `&`.

To obtain a replacement URL:

1. Open the [official Viz Builder](https://www.economia.gob.mx/datamexico/en/vizbuilder).
2. Select Foreign Trade → Foreign Trade (BCMM) by Municipality
   (`economy_foreign_trade_mun`).
3. Select Municipality, HS6, Flow, Date Year (serialized as `Year` in your URL),
   State, Product Level, and Country at the **Country** level. Keep State as a
   drilldown even though it is also a filter.
4. Set Product Level=6 and one known nonempty year/state/flow slice. Leave all
   municipalities, HS6 products and partner countries unrestricted.
5. Select Trade Value. If Number of Firms becomes available, select it there too.
6. Use the interface's API URL action and copy the **API request**, not the
   Viz Builder browser-page URL. Paste it as one line into `base_api_url.txt`.
7. Run the dry run, then a one-chunk live test before production.

The URL adapter supports the flat LogicLayer grammar in your actual copied URL.
It preserves original endpoint, parameter order, untouched encoding, and repeated
parameters. Only year/state/flow/product-level cuts and pagination are replaced.
Both `Year` and `Date Year` cut names are supported when present in the template.
Conflicting repeated cuts, unfamiliar parameters, additional product/country cuts,
top-N restrictions, monthly settings, and encoded `cuts[]` UI queries stop with
an explanation. It never silently guesses how to rewrite an unknown grammar.

Small test and production commands
----------------------------------

The safest initial test uses the slice already embedded in the copied URL:

```powershell
python download_bcmm_hs6_annual.py --api-url-file base_api_url.txt --test-live --output-dir bcmm_hs6_test
```

`--test-live` overrides year/state/flow CLI selections and uses the template slice
(currently 2017, state 01, flow 1). It downloads that entire chunk, not merely a
preview. Use ordinary filtering to test another slice:

```powershell
python download_bcmm_hs6_annual.py --api-url-file base_api_url.txt --start-year 2017 --end-year 2017 --states 01 --flows 2 --output-dir bcmm_hs6_test
```

Inspect the log, CSV, and `manifest.csv`. A nonempty successful chunk must have
`completed=1`, `terminal_empty=1`, `boundary_checks=1`, the expected year/state/flow,
and Product Level=6 on every row. The log prints the returned columns, observed
flow label, page counts, row counts, and whether Number of Firms was present.

Production command for the requested 2006–2025 scope:

```powershell
python download_bcmm_hs6_annual.py --api-url-file base_api_url.txt --start-year 2006 --end-year 2025 --states 01-32 --flows 1,2 --output-dir bcmm_hs6_annual
```

The initial review did not launch the national extraction. The subsequent
user-approved pilot/production workflow records its current status in
`bcmm_hs6_pilot/execution_report.json`. The initial live test's
disagreement with your earlier sample is unresolved; investigate it before using
either snapshot in suppression analysis. The production command is provided for
when you choose to scale the extraction. Including 2026 is possible with
`--end-year 2026`, but does not certify that year as complete.

Useful options: `--page-size 1000`, `--pause 0.5`, `--max-retries 6`,
`--timeout 90`, `--verbose`, `--force`, and `--require-number-of-firms`.

Pagination and network handling
-------------------------------

The startup probes use the known nonempty template slice to distinguish the two
possible pagination orders and verify that offset 1 advances to the second row.
If both candidate orders return empty, both return data, or the offset behaves
inconsistently, extraction stops. A base URL for an empty slice cannot establish
pagination safely; use a known nonempty reference slice even when requesting
other, potentially empty chunks.

For each production chunk:

1. Start at offset zero and request a page using the verified order.
2. Validate all records and append them to a unique `.part` CSV.
3. Increase offset by the **actual number of records returned**. A short page is
   not a terminal page, even if a hidden server cap is smaller than the requested
   page size.
4. Continue until an explicitly empty record list is returned. Unexpected
   wrappers, malformed JSON, API errors, and non-200 responses cannot become
   empty pages.
5. Request two rows starting at the last published offset; require exactly the
   same last record and no following record. Replay the first page and require
   the same records and values. Only then commit the CSV and completion record.

Full-page hashes detect repeated pages, including reordered identical pages.
An on-disk SQLite primary-key index detects duplicates within/across pages using
year, state, municipality, source HS6 ID, partner-country ID, and flow. Duplicate
keys stop extraction with their locations; they are never dropped. RAM use is
bounded by pages and small manifest metadata, not the national dataset.

An empty chunk is checked again using a single-row request without an offset.
It produces a header-only CSV, `status=downloaded_empty`, and an explicit
`number_of_firms_present=unverified_empty`. Its header comes from the nonempty
reference query; no row-level assertions can be made about an empty response.

Requests are sequential. Connection/timeouts and HTTP 429/500/502/503/504 retry
with exponential backoff (capped at 60 seconds before any larger Retry-After
instruction). Both numeric and HTTP-date Retry-After values are respected.
Successful requests are separated by the configured pause. Persistent errors
stop the run and leave the failed attempt in the manifest. Interrupted chunks
are recorded and restart from offset zero.

These checks improve extraction integrity but **cannot prove source completeness**.
A server-side total-result cap, changing data beyond the replayed boundaries, or
unstable ordering that omits rows without duplicating any can evade pagination
checks. A live cube is not a transactional snapshot. Independent control pulls,
coverage checks, unit verification and reconciliation remain necessary. No row
cap, row-count total, or suppression threshold is fabricated.

Files, manifest, and restart behavior
-------------------------------------

```text
bcmm_hs6_annual/
  raw/
    bcmm_hs6_annual_y2006_s01_f1.csv
    ...
  responses/
    <chunk>.<attempt_id>.jsonl.gz
  manifest.csv
  logs/
    run_<id>.log
    preflight_<id>.jsonl.gz
```

The raw CSV retains all response columns under their original names. Gzipped
response archives retain each request URL, timestamp, status/error, and complete
response text, including source metadata, nulls, data types, and validation
requests. This also preserves information CSV alone cannot distinguish, such as
JSON null versus an empty string. Preflight evidence is stored separately.

The manifest is an atomically replaced CSV containing an attempt history:
`started`, `failed`/`interrupted`, or `downloaded`/`downloaded_empty`. It records
product level, cuts, flow text, requested page size, actual row/page counts,
pagination order, terminal offset, boundary checks, retry/error counts, raw
column names, firms presence, file size, SHA256 checksums, response-archive path,
query URL/fingerprint, replacement history, and timestamps. `n_pages` counts
nonempty data pages; preflight and boundary requests are in the archives.

`completed=1` means the download protocol passed. It is **not** the guide's
research-ready `valid` status: `reconciliation_status=not_performed` is deliberately
retained. A downstream reconciliation stage can join by filename and chunk cuts,
then publish its own validation table without editing raw data. Differences must
remain unresolved until investigated; no control-query mode is invented here.

Rerun the same command to resume. A completed chunk is skipped only after its
query fingerprint, raw-file checksum/size/header/row count, response-archive
checksum, and completion evidence match the manifest. Completed chunks are not
downloaded again. Flow labels from successful prior attempts are restored and
checked against subsequent data. If every selected chunk verifies, no network
request is made.

A missing file, changed file, orphan final CSV, or changed query stops with an
actionable error. Use a new output directory for a different query/snapshot, or
inspect the problem and deliberately rebuild using `--force`. Select narrow
year/state/flow cuts if only one chunk needs rebuilding. `--force` keeps the
previous CSV until its replacement passes; failures leave the previous version
intact. It records the previous SHA256 and retains prior response archives. A
crash between CSV replacement and manifest commit becomes an orphan/hash mismatch
requiring investigation, never an automatically accepted completion.

Atomic CSV/manifest replacement retries brief Windows access/sharing locks with
bounded backoff, keeping the old destination intact. A persistent lock still
stops clearly; close any application holding the file before resuming.

Failed `.part` files and SQLite indexes are retained for inspection. A retry
uses a new attempt ID and starts from zero. A directory lock blocks concurrent
writes. Normal completion and Ctrl+C remove it. After a process/host crash,
confirm no downloader is using the directory, then remove only `.download.lock`
before restarting. No automatic stale-lock deletion is attempted.

Identifiers, flow labels, and missing cells
------------------------------------------

JSON numeric tokens are parsed as their exact text, so neither identifiers nor
large values pass through binary floats. String IDs such as `010121` remain
unchanged. CSV fields are quoted, but CSV has no type schema: downstream tools
must import IDs as strings (for example, pandas `dtype=str`, or Stata
`import delimited ..., stringcols(_all)`). Opening a CSV directly in Excel may
still cause Excel to remove leading zeros; that is not an appropriate raw edit.

Raw geography IDs are preserved as returned (`1001` and `1` in the live API).
For validation and filenames, municipality/state keys are compared as
`01001`/`01`. Later analytical copies can derive `mun_id = raw.zfill(5)` and
`ent_id = raw.zfill(2)`, while retaining raw IDs. Existing leading zeros are never
removed from raw fields.

The sample has **7- and 8-character source HS6 IDs**, such as `1020312`, despite
Product Level=6. These are not six-character standard HS codes. The downloader
does not slice off prefixes, cast them to integers, or fabricate a conversion.
A verified product metadata crosswalk is required before creating a standard
six-character `hs6` variable for analysis.

Live probes of the supplied slice observed `1 → Imports` and `2 → Exports`, the
reverse of the primary PDF's assumed numbering. Neither semantic mapping is
hard-coded. Both ID and text are preserved; inconsistencies stop the run. Other
flow IDs can be supplied explicitly if the live categories change.

Only published records are written. Missing combinations are not generated,
zero-filled, or classified as suppression. Number of Firms is never summed or
imputed. Movement-cube matching, Stata conversion,
currency conversion, and suppression analysis are outside this downloader.

To require Number of Firms later, obtain a fresh copied URL selecting both
measures and run with `--require-number-of-firms`. The program checks both the
query and every nonempty response schema. Merely adding the flag to the present
Trade Value-only URL fails before downloading. If a copied URL requests Number
of Firms, it is required in responses even without the flag. Presence does not
guarantee non-null counts; the raw values remain available for later diagnostics.

Parquet is optional downstream: it can reduce storage and improve analytical
reads, but introduces dependencies and requires explicit string ID types.
Keep immutable CSV and response archives as the provenance layer. No mandatory
consolidated CSV or Parquet copy is created.

Review evidence
---------------

See [REVIEW.md](REVIEW.md) for draft defects, source precedence, live findings,
and the bounded sample comparison performed during development. Raw review
artifacts and source datasets are intentionally excluded from this repository.
The offline test suite uses generated records and does not require those files.

Approved pilot-to-production workflow
------------------------------------

`run_pilot_then_production.py` runs the approved 12-chunk pilot in
`bcmm_hs6_pilot/`: 2017 in states 01/02/08/19 with both flows, plus 2006 and 2025
in state 01 with both flows. It independently revalidates the files and checks
uniqueness on disk, repeats the largest chunk at a different page size in
`bcmm_hs6_pilot_alternate_pages/`, compares every field without assuming row
order, and reruns the pilot with network access disabled to test resume.

Only after these checks pass does it invoke the 1,280-chunk production pull into
`bcmm_hs6_annual/`, using 4,000-row pages and the existing sequential retry policy.
An empty pilot chunk stops for investigation. The report records source-CSV
checksums before/after, acceptance results, stage, and any stop reason. Successful
downloads remain marked with reconciliation not performed.

To run or resume this approved workflow:

```powershell
python run_pilot_then_production.py --source-csv "C:\path\to\export_sample.csv" --source-csv "C:\path\to\viz_builder_sample.csv"
```

Do not start a second instance while one is running. Existing validated pilot,
alternate-page, and production chunks are reused on restart. Failures stop the
workflow, with details in `execution_report.json` and the downloader's manifest
and log files. The original source CSVs are only read, never rewritten.
