# Cottonwoods snowfall forecasting: project context

Goal: build a calibrated snowfall forecast method for the Wasatch Cottonwood Canyons (Little/Big Cottonwood)
from archived deterministic + ensemble model output, verified against SNOTEL and the owner's snow journal.

## How the owner forecasts today (formalize this, don't replace it)
- Reads Pivotal Weather / COD Meteorology. Models used: HRRR, RAP, NAM, NAM-Nest, RDPS, GDPS, GFS, ECMWF, ICON;
  ensembles GEFS, ECMWF ENS, GEPS.
- Snowfall = QPF x SLR, with SLR from **Kuchera** (not fixed 10:1). Wants a better SLR using low-level temp,
  mid-level temp, evaporative cooling (wet-bulb), adiabatic cooling (terrain adjustment).
- Ranges:
  - 0-3 d: many high-res models, Kuchera totals, trends across runs, reconcile models, nudge with observations.
  - 3-7 d: totals are NOT trusted. Track trends and the pattern: upstream trough, downstream systems
    (slow/weak lows affect timing and flow), 500 mb vorticity, 500-250 mb wind (low winter jet),
    SLP + precip for low strength/position, 850 mb over the Plains/East Coast (damps diurnal heating).
  - 7-10 d: ensemble "x of N members put measurable snow/precip in the Cottonwoods" plus a 2-of-3 family
    consensus (GFS, ECMWF, GDPS) and trend/convergence of the solutions.
- Event definition for ensembles: any measurable snow in the Cottonwoods; for coarse models use area precip.

## Rules of thumb for this codebase
- Apply SLR **per output time step**, then sum. Never apply Kuchera to accumulated QPF.
- Handle accumulation windows per model (HRRR hourly, GFS 3/6 h buckets that reset, ECMWF cumulative from init).
- GEFS/GFS are not independent; count model *families* (GFS/GEFS, ECMWF/ENS, GDPS/GEPS) when judging agreement.
- Convert ensemble counts to fractions and compare to the climatological base rate (lift), not raw counts.
- 850 mb is underground over the Intermountain West; use 700 mb near Utah, 850 mb only east of ~100W.
- Canadian (GDPS/RDPS) public archives are short. The journal is the only long record for them.
- Terrain: model grid height at Alta is far below the real ~8,700-10,000 ft. Adjust temperatures to station elevation.

## Architecture (where things live; one job per module)
Data flow: **pull** (models/, obs/) -> **archive** (archive/) -> **derive** (derive_run_fields) -> **storms + verify** -> **reports**.

Config and shared
- `config/stations.yaml`: ALL settings. `config.load_config()` checks every required key (`config.REQUIRED`) and fails
  loudly; code has NO fallback defaults for required settings. Optional sections (synoptic, ghcn, soundings,
  model_cells, catchup_days) are read with `.get`.
- `timefmt.py`: every time shown to people: Mountain (MDT/MST by date), 24-hour. Data stays UTC.
- `windows.py`: the one window-total rule (strict for daily report, prorated for storms).

Pulling model runs (`models/`)
- `registry.py`: every model's schedule (cycles 00/06/12/18z shared; GDPS 00/12z), forecast hours, posting lag,
  ensemble size, server STREAMS, and `get_puller`.
- Pullers, all returning the same columns (`fields.py` lists them): `hrrr.py`, `gfs.py` (+ `gfs_gdex.py` NCAR subsets,
  AWS fallback), `herbie_generic.py` (NAM, NAMNEST, IFS, and ensembles GEFS/ENS precip-only with `member`),
  `canada.py` (RDPS/GDPS, MSC Datamart, ~30-day retention). `accum.py` turns precip buckets into steps.
- `fields.py`: field lists, `point_fields`, `open_grib` (cfgrib.open_datasets; xarray.open_dataset silently drops
  wind), `parallel_hours` (download in threads, decode serially: ecCodes is NOT thread-safe).

Archive (`archive/`)
- `forecasts.py`: one file per run; `run_points` (resort points + coarse models' 2x2 `model_cells`); `archive_run`
  (completeness check); `derive_run_fields` = THE derivation entry point: regimes -> modes -> add_snowfall.
- `runner.py`: an archive pass: PassLock, build_jobs (newest first; Canada 3-day catch-up), FailureLog
  (skip after 4 tries), run_jobs, split_streams/run_streams (one process per server), clear_caches.
  `scripts/archive_forecasts.py` is only its command line.
- `obs.py`: SNOTEL (UTC-8!), Synoptic, NOAA coop, soundings stores; `drop_unreliable_water`.

Derivation (pure functions, all tested offline)
- `profile.py`: profile at the station's real elevation, wet-bulb snow/mix/rain (NWS WR TA 19-01), dry-air nudge.
- `slr.py`: Kuchera. `regimes.py`: per-step 700 mb flow, storm phases, resort regime factors.
- `modes.py`: precip mode per step (frontal / post-frontal / cellular / lake-effect).

Storms, verification, reports
- `storms.py`: storm life cycle - forecast -> verified, or dropped (future storm no newest run shows) / merged; splits
  noted ('split from'); per-model separate periods (fc_periods) and sub-periods (6 h gaps, fc_subperiods).
  Detect events (12 h dry gap), storm windows (LONGEST deterministic event), storm table, verify
  against SNOTEL once 12 h dry, scores. `verify.py`: the separate DAILY-window verification (24 h windows ending
  12 UTC; NOAA ALTA at its 6 pm). Both are intentional: storms = event totals, verify = day-by-day skill.
- `report.py`: the daily model table, staleness warnings, and the storm report. Numbers and text are separate:
  `model_days` / `storm_models` compute, `model_table` / `render_storms` format; the feed reuses the numbers.
- `feed.py`: website feed v1 (meta/storms/daily .json, Mountain ISO times, inches, null for missing);
  `release.py`: the approval gate; `github_feed.py` pushes an approved feed to GitHub Pages. `scripts/publish_feed.py` (last step of run_models.bat; `--preview` for any code
  -> data/feed_preview/). `config.DATA_DIR` honors COTTONWOODS_DATA so a production copy shares one data folder.

Observation clients (`obs/`): `snotel.py` (AWDB), `synoptic.py` (CLN, needs token), `ghcn.py` (NOAA coop),
`sounding.py` (SLC balloons via IEM). Scheduled (until the cloud cutover): Windows tasks "Cottonwoods models" (every 3 h, run_models.bat) and
"Cottonwoods soundings" (8 am/8 pm), both via `conhost --headless` (no window to close by accident).

## How to change this code (keep it maintainable)
- Change behavior in ONE place: a new derived field goes in derive_run_fields' chain, a new model in registry.py +
  a puller returning the `fields.py` columns, a new threshold in config (and `config.REQUIRED`), never a code default.
- Edit, don't rewrite: modify the function that owns the behavior; don't add a second copy "for now".
- Behavior-preserving refactors: snapshot outputs first (re-derive archived runs + render both reports), refactor,
  confirm identical. Intended behavior changes are reported to the owner separately.
- Every fix or feature gets an offline test in the matching `tests/test_<area>.py` (helpers in `tests/helpers.py`);
  `python tests/run_offline.py` finds all test files. Lint: ruff `--select F,E9` must be clean.
- Never decode GRIB in threads. Never script alta.com. Keep process counts low (8 cores / 7.8 GB RAM).

## Once per session: maintenance review (owner's standing request, 2026-10-27)
Before finishing a working session, check the code didn't get messier, and clean up if it did:
1. Lint + dead code: `ruff check src scripts tests --select F,E9` and `vulture src scripts --min-confidence 60`
   (installed into a scratch folder with `pip install --target`, not the owner's environment).
2. Structure: functions over ~60 lines, scripts holding logic that belongs in src/, duplicated helpers, modules doing
   several jobs, config values duplicated as code defaults.
3. Fix behavior-preserving: `python tools/golden.py save` first, then `check` after every step (must be IDENTICAL);
   `python tests/run_offline.py` all passing; report intended behavior changes separately.
4. Update this file (Architecture / Status) and tell the owner what was found and fixed (or that nothing was).
5. Commit the session's work as a checkpoint (owner's standing rule): `git add -A && git commit` with a message
   summarizing the session, then report `git diff --stat <previous checkpoint>..HEAD` to the owner.

Watch list from the last review (2026-10-27): functions just over 60 lines - report.render_storms (split it when the
timeline section is added), gfs._pull_run_aws, ghcn.fetch, profile.station_profile (cohesive; fine for now).
era5.request_pressure_levels is unused on purpose (planned ERA5 regime work).

## Status (2026-10-27)
Running live: all nine models archived every 3 h with station-profile snow, regimes, modes, 2x2 cells; observations
(SNOTEL, NOAA ALTA, soundings); storm tracking + verification; reports in data/reports/. Verified facts: SNOTEL hourly
is UTC-8 (PST) all year; NOAA ALTA water before 10/2007 unreliable; Collins (CLN) published 12 h data exists
(Hive doi:10.7278/S50d-nsy5-8bje, CC BY-NC). Owner rules are in memory and in config comments.

## Open questions for the owner
- Which site were the journal "actual" totals measured at (Alta, Snowbird, Brighton, LCC avg)?
- Confirm storm window start/end times in `data/journal/storms.csv` (`window_confirmed=false` rows).
- (Resolved 2026-10-26: the 11/23, 12/1, 12/8 and 4/15 storms are the 2023-24 season.)
- Actuals missing: 2/16-2/18/2026 storms and 3/5/2026.

## Next build steps (agreed order)
1. DONE 2026-10-27: stale-storm fix + merge/split/periods/parts.
   NEXT: REORGANIZED STORM REPORT, owner chose BOTH formats (2026-10-27): data/reports/storms_latest.html (opened in a
   browser, rebuilt each pass, works offline: clean tables, color-coded bins, trend arrows, collapsible details) AND
   the text version (log/history). Per storm, SUMMARY FIRST: window, confidence (ensemble agreement + deterministic
   spread), flow/snow level/mode one-liners; per-resort table: owner's bin (lowest containing the median), median,
   model range, trend since last cycle; timeline table; "changes since last cycle" sentence; details below
   (today's per-model table, phases, modes, parts). Split report.render_storms (77 lines) while doing this.
   Built together with: a STORM TIMELINE + RUN-TO-RUN TRENDS section in the storm report, per model per resort:
   snow ONSET (first 3 h-averaged snow rate >= ~0.1"/h; snow, not precip), PEAK snowfall rate time + in/h (3 h
   average, so 1 h and 3 h models compare fairly), snow END, snow/water totals; trends vs the model's previous run
   (onset/peak/end hours earlier/later, snow +/- in and %, water, snow level) and over the last 3-4 runs
   (convergence); ensembles: wet fraction + mean water by run. Mountain 24 h times. Uses existing data only.
   ALSO (owner, 2026-10-27): show NOAA ALTA (USC00420072) OBSERVED precip prominently in the reports - daily water
   (PRCP), new snow (SNOW), depth, and per-storm totals, labeled with its observer day (24 h ending 1800 MST) - so the
   owner can compare it by eye with Alta Collins on https://www.weather.gov/wrh/timeseries?site=CLN&hourly=true
   (viewing only; never scrape that page, it uses an NWS-issued Synoptic key).
Synoptic: skipped for now (free tier is now a 14-day trial). Collins live data stays configured but idle; Collins
history comes from the Hive 2000-2023 dataset. Possible later: NWS SLC public information statements (storm totals
incl. Alta, archived as text) as an automated Alta-total source.
3. Better snow ratio: import the Collins dataset; Veals et al. 2025 V1/V3 (github pveals/Veals_etal_2025) next to
   Kuchera; Alta-only retrain; fit the wet-bulb thresholds and dry-air nudge to Collins + SLC soundings.
4. Owner's bins in the reports (lowest bin containing the estimate) and bin verification (in / over / under).
5. Storm-day selector + backfill past storms.
5b. PATTERN archive (owner approved 2026-10-27; start early so a season accumulates before step 6): coarse regional
   fields for the long-range models (GFS, ECMWF HRES, GDPS + GEFS/ENS mean & spread): 500 mb height + vorticity,
   250 mb wind, MSLP, 850 mb temp, every 6 h, ~0.5-1 deg, box ~25-60N x 150-90W (upstream troughs to Plains).
   Few MB/run; server-side subsetting is the right tool here. Enables trough position/timing comparisons between
   models and runs (owner's 3-7 day "trend/convergence" method), pattern-based regime labels days ahead, and
   weighting models by pattern skill verified against analyses + SLC soundings. Point archive stays as is.
6. Learning layer last: per-model bias by lead/regime and by PATTERN (from 5b), weighted blend, calibrated ensembles, tuned pessimistic bin;
   the program's own frozen "CWX" forecast each cycle, scored like a model. Keep only what beats raw models.
WEBSITE FEED (owner, 2026-10-27; after the HTML report, shares its summary data): a STATIC JSON "API" written after
   each pass (e.g. storms.json, storm/<id>.json, latest_runs.json, noaa_alta.json, scores.json) and published to a
   free static host (GitHub Pages / Netlify / Cloudflare Pages); no always-on server (laptop sleeps). Open questions
   for the owner: audience (private / friends / public), existing site or build one, host account + token for
   auto-publish (confirm before anything goes public). Public needs attribution (ECMWF, NCAR GDEX, MSC), Collins
   CC BY-NC (non-commercial), "not an official forecast" disclaimer, nothing from alta.com.
   RELEASES before going live (owner, 2026-10-27): the site must not change just because code is being developed.
   Scheduled passes + feed run from a PRODUCTION folder (git worktree pinned to an owner-approved tag, e.g. v1.0);
   development continues in main; owner approves each release (show git diff first). Feed carries schema_version /
   /v1/ paths: additive changes only within v1, breaking changes as v2 alongside v1. Contract tests pin the v1 format.
   Archive/data stay shared between folders so a release never loses history.
   APPROVAL GATE (owner, 2026-10-27): ONLY owner-approved versions may feed the API. The publish step refuses to run
   unless its code is an owner-approved tag (e.g. v1.0, made by the owner or on the owner's explicit "approve") with no
   uncommitted changes; development code in main can never publish, even if run by hand. Forecast DATA still refreshes
   every pass; only the CODE producing it is frozen to the approved release.
   BUILT 2026-10-27: feed.py, release.py, scripts/publish_feed.py, tests/test_feed.py (v1 contract + gate). Not live.
   RELEASE PROCEDURE (only on the owner's explicit "approve", after showing `git diff <last tag>..HEAD --stat`):
     1. Make sure the release commit has config `feed_github` switched on (it's commented out until the first release).
     2. `git tag -a vX.Y -m "Approved by owner <date>"`, then `git push origin main vX.Y`.
     3. The next GitHub Actions pass runs that tag and publishes; check the run log shows "Running approved release".
   PUBLISHING (2026-10-27): friend declined to share VPS access, so the feed is PUBLIC on the owner's GitHub Pages:
   repo github.com/CabbagePatchKim/cwx-feed -> https://cabbagepatchkim.github.io/cwx-feed/v1/{meta,storms,daily}.json;
   the friend's site fetches it. github_feed.py: clone in data/feed_repo, copy, commit, push with deploy key
   ~/.ssh/cwx_feed_ed25519 (write access, this repo only). Config feed_github stays commented until the owner adds
   the deploy key, turns on Pages, and approves the first release. Never push preview/dev output.
CLOUD PASSES (owner approved 2026-10-27): passes move from the laptop to GitHub Actions (.github/workflows/pass.yml,
   every 3 h at :30 UTC, newest approved tag else main) in a PUBLIC code repo (free minutes), archive in a Cloudflare
   R2 bucket (bucket.py / scripts/bucket_sync.py pull|push; never deletes; pulls skip runs > 25 days old).
   tests.yml runs the offline tests on every push. Secrets: R2_BUCKET, R2_ENDPOINT, R2_ACCESS_KEY_ID,
   R2_SECRET_ACCESS_KEY, FEED_DEPLOY_KEY (~/.ssh/cwx_feed_ci_ed25519, separate from the laptop's feed key).
   Laptop pushes code with ~/.ssh/cwx_code_ed25519 (deploy key on the code repo). Commit emails rewritten to the
   owner's GitHub noreply address before the repo goes public. Cutover: seed the bucket from the laptop (push),
   run both for a day and compare, then disable the laptop tasks "Cottonwoods models" + "Cottonwoods soundings";
   afterwards the laptop is development only (`bucket_sync.py pull` for live data).
   Cloud pass output is logged to the bucket: data/logs/cloud/models_YYYY-MM.log (header per pass with code version
   and run URL, a step-results line at the end). START OF A SESSION: pull, then read that log for failures.
Deferred: server-side subsetting (NOMADS / GeoMet), only if bandwidth becomes a problem.
