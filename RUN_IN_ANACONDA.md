# Run from Anaconda Prompt

## HS2: separate annual and monthly downloads

From the project directory, run:

```bat
python run_download.py --product-level 2 --frequency annual
python run_download.py --product-level 2 --frequency monthly
```

Annual output: `bcmm_hs2_annual` (1,280 chunks). Monthly output:
`bcmm_hs2_monthly` (15,360 chunks). Both select 2006-2025, 32 states, both flows,
Product Level=2 and the independent `HS2 2 Digit` hierarchy. Product columns become
`HS2 ID` and `HS2`; monthly files additionally retain `Month` as `YYYY-MM`.

Keep the existing `base_api_url.txt`. Append `--dry-run` to check configuration,
repeat the same command to resume, or append `--retry-failed` to retry failures.
HS4 and HS6 filenames and resume fingerprints are preserved. The ongoing HS4
monthly process does not need a restart, and each HS2 mode writes to its own directory.

After annual downloading finishes successfully, combine the validated chunks:

```bat
python combine_bcmm_chunks.py --product-level 2
```

## HS4: separate annual and monthly downloads

Run these commands from this project directory in Anaconda Prompt:

```bat
python run_download.py --product-level 4 --frequency annual
python run_download.py --product-level 4 --frequency monthly
```

They write to `bcmm_hs4_annual` and `bcmm_hs4_monthly`, respectively. Each keeps
its own chunk files, manifest, failure ledger, archives, and logs. Annual mode
selects 1,280 year/state/flow chunks; monthly mode selects 15,360 month/state/flow
chunks. Both use 2006?2025, all 32 states, both flows, and Product Level=4.

Other column names stay the same; product columns are `HS4 ID` and `HS4`.
Monthly files append `Month` (`YYYY-MM`). Keep the existing `base_api_url.txt`.
The code queries the independent `HS4 4 Digit` hierarchy directly and retains
unmodified API responses in the archives.

Append `--dry-run` to check settings without downloading. Repeat the same
command to resume, or append `--retry-failed` to retry that mode's failures.
The timeout, pacing, retries, and validation rules below apply to HS4 too.

For **monthly** downloads, use:

```bat
cd /d "C:\path\to\Municipal-Project"
python run_download.py --frequency monthly
```

Replace the directory with your local project path.

This selects **15,360 month/state/flow chunks** for 2006–2025 and stores them in
`bcmm_hs6_monthly`. Each CSV retains the annual columns and appends `Month`
(`YYYY-MM`). `Year` is derived from the verified source month. Original source
Month IDs remain in the response archives. Keep `monthly_bcmm.py` beside the
other Python files and keep the existing annual `base_api_url.txt`.

Check the configuration or download a single monthly chunk first:

```bat
python run_download.py --frequency monthly --dry-run
python run_download.py --frequency monthly --start-year 2017 --end-year 2017 --months 01 --states 01 --flows 1
```

Resume the full monthly queue with the original monthly command. Retry only
recorded failures with:

```bat
python run_download.py --frequency monthly --retry-failed
```

The retry, pacing, validation, interruption, and exit-code rules below apply to
both modes. For monthly mode, read output paths below as `bcmm_hs6_monthly`.
Month numbers accept ranges or lists, such as `--months 01-06` or `--months 01,07`.
Use a separate directory for each frequency. Empty chunks remain explicitly
marked empty and require coverage investigation. Reconciliation is still pending.

## Combine completed monthly chunks into five-year files

For HS4, run after the monthly queue is complete and the failure list is empty:

```bat
python combine_bcmm_monthly.py --product-level 4
```

This writes four CSVs to `bcmm_hs4_monthly/combined_5year` for 2006-2010,
2011-2015, 2016-2020, and 2021-2025, plus `combined_manifest.json` with checksums
and row totals. Each CSV retains the monthly observations and all original
columns, including `Month`; values are not summed. Source chunks remain intact.
The merger fully validates the source files and archives before staging outputs.
Use the matching `--product-level 2` or `6` only after that monthly queue is complete.

## Annual downloads

Open **Anaconda Prompt** and run:

```bat
cd /d "C:\path\to\Municipal-Project"
python run_download.py
```

If `requests` is not installed, install the project requirements once:

```bat
python -m pip install -r requirements.txt
```

The program reads `base_api_url.txt` from the same directory and writes results
to `bcmm_hs6_annual`. By default, it downloads **2006–2025, all 32 states, both
flows, Product Level 6, and Trade Value**. Keep `run_download.py`,
`download_bcmm_hs6_annual.py`, and `base_api_url.txt` in the same directory.

## Automatic processing

- Completed chunks are skipped only after the manifest, query fingerprint, CSV
  SHA256, row count, header, and response archive all pass validation. Existing
  compatible output is reused.
- The connection timeout is **60 seconds** and the read timeout is **300
  seconds**. The read timeout applies to an individual network read, not to the
  total chunk runtime, so a large chunk may paginate for hours.
- Requests are separated by at least **2 seconds**, and new chunks by **10
  seconds**.
- A temporary page-level network error is retried after **10 and 20 seconds**.
  A longer server `Retry-After` value is respected.
- If page retries are exhausted, a response is corrupt, pagination is invalid,
  keys are duplicated, filters do not match, or validation fails, the whole
  chunk attempt is discarded. The chunk restarts at offset zero after waits of
  **20, 40, 80, and 120 seconds**, then 120 seconds for later retries. Each
  chunk receives at most **10 complete attempts per run**.
- A chunk that still fails is recorded in `failed_chunks.csv`. Processing then
  continues with the next chunk. A partial CSV is never accepted as complete.
- Pressing Ctrl+C stops the current run while preserving completed chunks. Run
  the same command again to resume. A lock left after an abnormal shutdown is
  removed only after the program confirms that its process is no longer active.

Global configuration errors, failed startup pagination checks, insufficient disk
space, a corrupt manifest, and persistent write-permission failures stop the run
because they can affect every chunk. If a previously completed file is missing or
modified, the program records it for investigation and continues with other valid
chunks without overwriting that file automatically.

## Automatic validation

The downloader verifies pagination order at startup and checks Product Level 6,
year, state, flow, required source columns, source IDs, and logical-key
uniqueness. It rejects repeated pages, advances the offset by the actual returned
row count, requires a terminal empty page, and then validates the last record and
replays the first page. The final CSV is committed only after all checks pass.

Source IDs are preserved as strings. Missing values are not changed to zero, and
source HS6 IDs are not truncated to six characters. Number of Firms is currently
outside the requested extraction. These checks validate download integrity and
consistency; independent control-total reconciliation, unit checks, and
suppression analysis remain pending. The manifest therefore retains
`reconciliation_status=not_performed`.

## Results and failed-chunk retries

Outputs are written under `bcmm_hs6_annual`:

| File | Purpose |
| --- | --- |
| `raw\*.csv` | Completed raw chunks |
| `manifest.csv` | Source, row count, checksum, and status for every attempt |
| `failed_chunks.csv` | Unresolved chunks with cuts, error, URL, archive, and log paths |
| `chunk_attempts.csv` | Persistent attempt history, including failures later resolved |
| `run_summary.json` | Counts, current chunk, and run status |
| `logs\standalone_*.log` | Complete terminal log |

Failure details are saved on the first failed attempt, so an interruption does
not lose diagnostic information. A later success removes the chunk from the
current failure list while preserving its attempt history.

After a full run, retry only unresolved chunks with:

```bat
python run_download.py --retry-failed
```

Each selected failed chunk receives up to 10 new attempts. To resume all work,
including chunks that have not started, use `python run_download.py`.

To use longer network waits and wider pacing:

```bat
python run_download.py --read-timeout 600 --request-pause 3 --chunk-pause 20 --retry-wait 30 --retry-wait-max 180
```

To display the resolved configuration without network access or downloads:

```bat
python run_download.py --dry-run
```

Exit codes are: `0` when all selected chunks succeeded or were validly skipped;
`1` when the queue finished with unresolved chunks; `2` for a global
configuration, storage, or startup error; and `130` for a user interruption.

Use **`run_download.py`** for current runs. `run_pilot_then_production.py` is
retained as the earlier pilot workflow and is not required for routine downloads.
