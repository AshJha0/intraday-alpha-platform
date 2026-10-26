# DD1 ingestion runbook: the free daily route into the four partitions (owner)

v2.0 stage 2 DD1 (U0 section 2 partitions, section 3 "Stage 2" DD1, G3, G4, DR-6, DR-7). Code:
[`tools/ingest/dd1_daily.py`](../../tools/ingest/dd1_daily.py) (the job), the `ingest` endpoint of
[`tools/sealed_service.py`](../../tools/sealed_service.py), [`tools/data_manifest.py`](../../tools/data_manifest.py)
(`kind: daily_pit` entries), the install scripts under [`tools/admin/`](../../tools/admin/), the `dd1` mode of
[`.github/workflows/registered-run.yml`](../../.github/workflows/registered-run.yml). Tests on synthetic vendor-format
fixtures: [`python/tests/ingest/`](../../python/tests/ingest/), `python/tests/isolation/test_sealed_ingest.py`.

Nothing in this repository has ingested anything. The run below is the owner's, executed by the `iap-runner` identity
on the workstation after an environment approval; the development identity (the owner's normal user, the sessions that
write code) never touches a sealed partition and never sees a sealed value, before or after.

## 1. What the job does

| step | who | what | what you see |
|---|---|---|---|
| identity | `iap-runner` | refuses to run as any other account (exit 3) | log line `refused` |
| pull | `iap-runner` | Stooq bulk daily archive, read from your verified download (step 4; or Tiingo if `TIINGO_API_KEY` is in the runner's environment); every Wikipedia revision of the S&P 500 and Nasdaq-100 pages (last per day); EDGAR `form.idx` per quarter for the universe's CIKs; the three French daily factor files; Tiingo's public ticker list for listed last dates | counts per source |
| cut | `iap-runner` | development = to end of `Y-4`, validation = `Y-3`, Track B = `Y-2`, Track C = `Y-1` onward, `Y` = `--year` (the ingestion year) | the windows in the record |
| test | `iap-runner` | the five DD1 tests on the **development partition only**: survivorship, corporate actions, calendar, open/close convention, membership intervals (section 4) | `dd1 test <name>: pass` or `FAIL` |
| write | `iap-runner` | development and validation to `C:\iap\daily\dd1_<Y>\` (you can read them); Track B and C through `\\.\pipe\iap-sealed-b` / `-c` `ingest` (the service writes, re-hashes, records, answers hashes and counts) | `partition b: sealed in b, 5 files, chain head …` |
| record | `iap-runner` | `C:\iap\daily\dd1_<Y>\dataset.json`: hashes, row counts, vendor metadata, test block, `status` | `record written: status complete` |

"Complete" means: every DD1 test passed, both sealed services accepted their partition, the record says
`"status": "complete"`. Anything else is `incomplete`: the development partitions are still written (so the failure
can be examined on them), nothing is sealed, the workflow step fails, and a re-run needs a new purchase year (the job
refuses to overwrite a record; delete `C:\iap\daily\dd1_<Y>` yourself as the administrator if the run must repeat).

## 2. The sequence, once

1. **Python for all users and the checkout outside your profile**:
   [`tools/admin/install_all_users_python.md`](../../tools/admin/install_all_users_python.md) (CPython 3.12.10 at
   `C:\Python312`, the pinned stack, the clone at `C:\iap\repo` read-only to the service accounts).
2. **The three sealed services** (elevated PowerShell, from `C:\iap\repo`; asks for each sealed account's password):

```powershell
Set-Location C:\iap\repo
.\tools\admin\install_sealed_services.ps1
```

   It creates `C:\iap\daily` (iap-runner writes, you read), grants each sealed account "Log on as a batch job", runs
   `sealed_service.py install` and `check` **as the account** (you have no access to the partition, by design),
   registers "IAP sealed service a/b/c" (run whether user is logged on or not, at start-up, no time limit, restarted on
   failure), starts them and pings each pipe. Expected ping answer for you: `{"answered": true, "ok": false, "error":
   "caller_account"}` (only `iap-runner` is served; the refusal is audited in the partition).
3. **The runner**: [RUNNER_SETUP.md](RUNNER_SETUP.md) (work directory, registration as a Windows service under
   `iap-runner`, the `sealed` environment with you as required reviewer, the first-run checks of its section 6).
   Optional: `TIINGO_API_KEY=<key>` in `C:\actions-runner\.env` selects Tiingo; without it the job uses Stooq.
   **Required: `IAP_SEC_CONTACT=<your name> <your email>`** in the same file, then restart the runner service. The SEC
   answers 403 to any EDGAR request whose User-Agent has no contact email (its fair-access rule; run 40284491015 failed
   on it after 48 minutes). The job refuses to start without an email there (exit 3), and it calls EDGAR, French and
   Tiingo before the membership walk, so a refusing source stops the run in the first minute (D-88).
4. **Download the Stooq archive yourself** (DR-16, D-86). Since October 2026 stooq.com shows scripts a browser check,
   and the job does not try to pass it. In your browser, open <https://stooq.com/db/h/>, take the "U.S." daily ASCII
   archive (`d_us_txt.zip`, several hundred MB), and save it as `C:\iap\inbox\d_us_txt.zip`. Then, in PowerShell:

```powershell
New-Item -ItemType Directory -Force C:\iap\inbox | Out-Null
icacls C:\iap\inbox /grant "iap-runner:(OI)(CI)RX"
(Get-FileHash C:\iap\inbox\d_us_txt.zip -Algorithm SHA256).Hash.ToLower()
```

   Write down the printed hash and the date you downloaded the file. The job refuses before its first request if the
   file is missing, is not a zip, or does not match that hash, so a bad file cannot waste the four-hour membership pull.
   The record stores the hash and `archive_origin: owner_browser_download`.
5. **Probe first (a few minutes)**: run the workflow with `mode` = `dd1-probe` and the same other fields as step 6.
   It checks your zip and SEC contact, then makes one request of each kind the long run makes (Wikipedia revision
   list and one revision per index, EDGAR's ticker map and the 1993 Q1 and current quarter indexes, the three French
   files, Tiingo's ticker list), asks each sealed service for its status as the runner, and checks it can write to
   the output and cache folders. It writes no data. The last log line is `probe passed`; anything else names the
   source that refused (D-100). Start the full run only after a passed probe.
6. **Trigger the ingestion**: GitHub -> Actions -> `registered-run` -> Run workflow with `mode` = `dd1`, `commit` = the
   merge commit of the PR that added this runbook (or any later `main` commit), `prereg` = anything (ignored in this
   mode), `year` = empty (this year), `stooq_sha256` = the hash from step 4 (leave it empty only when Tiingo is set). A failed run can be started again with the same
   fields: busy answers are retried with back-off (D-96), parsed Wikipedia revisions and finished EDGAR quarters are
   read from the runner-only cache (D-87, D-97), and a partition the sealed service already holds is not sent twice
   (D-99), so a re-run resumes rather than starting over.
   The run waits in the `sealed` environment until you approve it; approve it.
7. **Read the redacted log** (the only artifact; paths, accounts and values are redacted). The last lines are the
   per-partition results and `record written: status complete`.
8. **List the record in the manifest** (development identity, your checkout):

```powershell
Copy-Item -Recurse C:\iap\daily\dd1_2026 data\real\daily\dd1_2026
python tools/data_manifest.py generate --data-root data/real
python tools/data_manifest.py check --data-root data/real
```

   The entry carries the four `dataset_hash_id`s (Track B and C from the services' responses), the row counts the job
   itself produced, the vendor metadata and the test block; commit it in a PR titled "DD1 ingestion record <Y>".

## 3. What to send the coordinator

- the workflow run URL and the redacted log's last ten lines;
- the four `dataset_hash_id`s and `status` from `dataset.json` (never the CSV files);
- the `dd1_tests` block (pass/fail and counts only);
- for a failed test, the counts of the failing test and nothing else: a failing survivorship or membership test
  downgrades the daily families to data-ineligible (DR-6 stop condition); it does not relax the test.

## 4. The five tests (development partition only; counts, never values)

| test | passes when | default (defect register) |
|---|---|---|
| survivorship | every name that left the index inside the window has a price row on every calendar session from its first interval start to its last interval end, and after it through 60 sessions (D4's horizon) or its listed last date (Tiingo's public ticker list), whichever is first; failures at most 2 % of that population | D-59 |
| corporate actions | Tiingo: every `splitFactor != 1` day reconciles raw and adjusted closes; Stooq (adjusted series only): no residual close-to-close jump of 40 % or more at a simple split ratio (2, 3, 4, 5, 3/2, 1/2, 1/3, 1/4, 1/5, 2/3, 3/4) within 2 % | D-63 |
| calendar | the panel's sessions (dates on which at least half the month's active names have a row) equal the French factor files' dates inside the panel's range; no weekend rows | D-62 |
| open/close convention | the convention text for the source is written into `metadata.json` (G4 for D3) and the share of opens equal to the previous close is at most 5 % | D-60 |
| membership intervals | per (index, symbol) intervals ordered and non-overlapping; every member has a price row inside an interval; a year counts as reliable when at least 90 % of its parsed revisions are plausible and the median gap between reliable revisions is at most 45 days; years below that are listed as excluded for membership-dependent families; the test fails only when no year in the window qualifies or an interval is malformed | D-42 |

Positive controls (coverage report section 5 item 6) run in CI: a truncated delisting, an unadjusted split and dropped
sessions each fail their test on the synthetic fixture, and a crafted overlapping interval fails the membership test.

## 5. What the run can and cannot show

The ingestion, the sealing and the tests run on one workstation whose administrator is the owner; the evidence class
of what they produce is **mechanically isolated** (SEALED_DATA.md section 4). The Wikipedia membership is a proxy
(every interval is labelled `wikipedia-proxy`); Stooq's series are vendor-adjusted with no raw or dividend table; the
EDGAR CIK map is today's; the exchange calendar is the French library's (CRSP) trading days. Each is a register row,
not a claim.
