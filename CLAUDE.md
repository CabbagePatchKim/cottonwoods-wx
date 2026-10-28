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
- `units.py`: MM_PER_IN, FT_PER_M (the only definitions).
- `timefmt.py`: every time shown to people: Mountain (MDT/MST by date), 24-hour. Data stays UTC.
- `windows.py`: the one window-total rule (strict for daily report, prorated for storms), and `within_lead` (the
  steps of a run a report shows). LEAD LIMITS (owner, 2026-10-27): storms detected + totalled from the first
  storms.max_lead_hours (168) of each run; daily table deterministic to report.deterministic_lead_hours (168),
  ensembles to report.ensemble_lead_hours (240: the 8-10 day "x of N" tail). ARCHIVE stays 240 h+ (IFS 360,
  GEFS 384) and verification scores every archived run at any lead. A storm tracked past the limit is dropped
  with "starts past ..., the lead limit" and revives as the same storm once a run reaches it.

Pulling model runs (`models/`)
- `registry.py`: every model's schedule (cycles 00/06/12/18z shared; GDPS 00/12z), forecast hours, posting lag,
  ensemble size, server STREAMS, and `get_puller(model, cfg)`, which binds the server's parallel downloads from
  config archive.download_workers (pullers have no worker defaults of their own).
- Pullers, all returning the same columns (`fields.py` lists them): `hrrr.py`, `gfs.py` (+ `gfs_gdex.py` NCAR subsets,
  AWS fallback), `herbie_generic.py` (NAM, NAMNEST, IFS, and ensembles GEFS/ENS precip-only with `member`),
  `canada.py` (RDPS/GDPS, MSC Datamart, ~30-day retention; files decoded from memory with ecCodes, grid cached). `accum.py` turns precip buckets into steps.
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
- `slr.py`: Kuchera. `regimes.py`: per-step 700 mb flow, storm phases, resort regime factors. Temperature: `band`
  (warm > -4 / mild > -10 / cold, config temp_bands_700mb_c) is what's shown and the finest factor level; the 2-way
  `thermal` (cold_700mb_c) is the factor fallback. factor_for: flow+band -> flow+thermal -> flow -> 1.0 (each needs
  factor_min_days). Archived runs are relabelled on read (with_bands), so changing band edges needs no re-archive.
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

Hand entries: data/manual/ (in git: new_snow.csv daily readings for verify.py) and data/entries/ (NOT in git,
bucket-synced so the cloud sees them without a release: storm_totals.csv, e.g. Alta Collins storm totals the owner
reads off the NWS page by eye, and collins_obs.npy/.csv: individual hourly + 12 h board readings, columns
obs.OBS_ENTRY_COLS; storm totals: non-overlapping boards (shortest first), a board's missing value filled from its
hours, hours outside every board added; sent with scripts/send_entries.bat). Storm report section "Verified in the last N
days" (report.render_verified) shows SNOTEL + hand totals vs each model's last run before the storm.
Collins reminder: SessionStart hook (.claude/settings.local.json, not in git) runs scripts/collins_due.py: storms
verified in the last report_verified_days with no Collins entries -> prompt the owner at the start of the session
(storm list from the bucket if reachable, else local). Offer to type the readings into data/entries for them.
Collins automation: NOT possible without a Synoptic token (api.weather.gov lists CLN but serves no observations;
the NWS page uses an NWS Synoptic key - never automate it, rendered or not). Laptop bucket access: boto3 in
~/.cwx_pylib (PYTHONPATH), not the main environment.

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

Watch list from the last review (2026-10-27, second review): functions just over 60 lines - gfs._pull_run_aws
(68), profile.station_profile (68), ghcn.fetch (62), regimes.phases (62): cohesive, fine for now. Speed: a pass is
bound by the NOAA stream (GFS ~12 min + GEFS ~7 min per run); GDPS is decode-bound (each file a whole-globe field),
so the remaining big speed lever is server-side subsetting (deferred: NOMADS filter / MSC GeoMet).
era5.request_pressure_levels is unused on purpose (planned ERA5 regime work). tools/golden.py keeps its own copy
of storms.csv, so a scheduled pass can't cause a false DIFF. Third review (2026-10-27, late evening, after the lead
limits): ruff clean, vulture only the era5 item, no new long functions; nothing to fix.

## Status (end of session 2026-10-27, evening)
Running live, TWO places for now (side-by-side check before cutover):
- LAPTOP: Windows tasks "Cottonwoods models" (every 3 h at :30 local, run_models.bat, DEVELOPMENT code = main) and
  "Cottonwoods soundings". Writes data/ locally. Never publishes (not a release; no CWX_PUBLISH).
- CLOUD: GitHub Actions github.com/CabbagePatchKim/cottonwoods-wx (PUBLIC), .github/workflows/pass.yml every 3 h at
  :45 UTC (was :30 until 2026-10-27), runs the newest approved tag = v1.0 (81d37f2). Archive in Cloudflare R2 bucket cwx-archive; log in
  bucket logs/cloud/models_YYYY-MM.log. Publishes the feed (CWX_PUBLISH=1 set only there).
  First manual run 2026-10-27 21:04Z: green, 53 min, but it ran main 77ddabc (before the v1.0 tag), so NOTHING
  is published yet: cwx-feed has only its initial commit; Pages is on (site root 200), /v1/*.json are 404.
  The 23:30Z scheduled run never fired (checked 00:20Z). Schedule moved to :45 and pushed 00:20Z 10/28.
- main is AHEAD of v1.0 (not released): 7a9350c lead limits (storms + deterministic daily to 168 h, ensembles to
  240 h), 59fca5b :45 schedule. Laptop runs main, cloud runs v1.0, so their storm lists differ (11/9 IFS 282 h
  storm dropped on the laptop only). v1.1 needs the owner's approve + a heads-up to the friend (daily.json
  deterministic days 8-10 become null; same format).
- FEED v1.0 (approved by owner 2026-10-27): MODEL-RUN DATA ONLY, public at
  https://cabbagepatchkim.github.io/cwx-feed/v1/{storms,daily,meta}.json (repo CabbagePatchKim/cwx-feed). The owner's
  friend's site reads those URLs. Release procedure: see "RELEASE PROCEDURE" below; laptop never publishes.
Keys/secrets (values never in git or chat): ~/.ssh/cwx_feed_ed25519 (laptop feed key), ~/.ssh/cwx_feed_ci_ed25519
(cloud feed key = GitHub secret FEED_DEPLOY_KEY), ~/.ssh/cwx_code_ed25519 (laptop push to the code repo; repo-local
core.sshCommand), ~/.ssh/known_hosts (GitHub host key, fingerprint SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU),
~/.cwx_r2.env (R2 credentials for laptop bucket access), ~/.cwx_pylib (boto3, kept out of the main env). GitHub
secrets: R2_BUCKET, R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, FEED_DEPLOY_KEY. Commit email = owner's
GitHub noreply (history rewritten before going public; pre-rewrite backup:
../../cottonwoods-wx_backup_before_public_20261027.bundle).
Verified facts: SNOTEL hourly is UTC-8 (PST) all year; NOAA ALTA water before 10/2007 unreliable; Collins (CLN)
published 12 h data exists (Hive doi:10.7278/S50d-nsy5-8bje, CC BY-NC); Collins live data has NO free automated
route (api.weather.gov lists CLN but serves no observations; never automate the NWS page).

## Tomorrow's agenda (start here, 2026-10-28)
0. FIRST THING (owner asked 2026-10-27): report on BOTH passes before anything else.
   a. Laptop: 10/27 17:30 MDT pass was checked: all five steps exit 0 by 17:35, feed refused to publish (correct);
      NAM 46 s, RDPS 154 s, GFS 216 s (old avg 756), ENS 271 s. Check the passes since (log after line 5466),
      especially GDPS and GEFS times (none in that pass). Old averages: GFS 756 s, GDPS 868 s, GEFS 405 s.
   b. Cloud: scheduled runs since 10/28 02:45Z (none had fired by 00:20Z; if none fired, ask the owner to press
      "Run workflow" once: it runs v1.0 and does the first publish) (api.github.com .../actions/workflows/pass.yml/runs), the first
      v1.0 publish, then fetch https://cabbagepatchkim.github.io/cwx-feed/v1/{storms,daily,meta}.json (code_version
      v1.0) and SEND THE OWNER THE THREE URLS (they're waiting for them for the friend's site).
1. Cloud health: did scheduled runs fire (Actions list via api.github.com, no login needed)? Pull the bucket
   (`scripts/bucket_sync.py pull`, Windows paths needed: PYTHONPATH="C:\Users\patri\.cwx_pylib",
   --env-file "C:\Users\patri\.cwx_r2.env"; the ~/ form fails with Windows Python) and read
   logs/cloud/models_*.log: per-step results, "Running approved release v1.0", "pushed ... cwx-feed". If no scheduled
   runs: investigate (GitHub delays/disables new schedules; manual "Run workflow" works). Also check how often a
   :45 pass now finds HRRR/NAM and GFS/GDPS already posted (the reason for moving it from :30).
2. Feed check: fetch the three public URLs; code_version must be v1.0; storms/daily sane. Give the owner the URLs.
3. Speed: compare per-run archive times before/after download_workers (laptop data/logs/models.log "----- <script>
   exit N (time)" lines + [archive] durations; cloud log). Laptop passes cut off after archiving earlier on
   10/27 (likely my edits/memory use during passes): confirm passes now run all five steps.
4. Side-by-side laptop vs cloud for a day, then CUTOVER (owner approval): disable laptop tasks "Cottonwoods models"
   and "Cottonwoods soundings"; laptop becomes development only (pull from the bucket).
5. Weekend storm (Fri 10/30 - Mon 11/2): first real verification ~11/2-11/3. The SessionStart hook will prompt the
   owner for Alta Collins readings (data/entries/, scripts/send_entries.bat).
6. Then the build plan below (HTML storm report + timeline/trends + NOAA ALTA display: DEVELOPMENT ONLY, unpublished;
   feed v1 stays model-only until the owner approves a release that adds anything).

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
   BUILT 2026-10-27: feed.py, release.py, scripts/publish_feed.py, tests/test_feed.py (v1 contract + gate).
   v1 = MODEL-RUN DATA ONLY (owner, 2026-10-27): no observations, no verification in the feed; feed.model_only()
   refuses any such key. Observation/verification work continues in development, unpublished. feed_github is on in
   config (only an approved tag can push).
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
   every 3 h at :45 UTC, newest approved tag else main) in a PUBLIC code repo (free minutes), archive in a Cloudflare
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
