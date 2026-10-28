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
  wind), `parallel_hours` (download in threads, decode serially: ecCodes is NOT thread-safe). THROTTLING (2026-10-28):
  ECMWF's S3 bucket answered the cloud pass with 503 Slow Down and ENS 18z came back with 624/4800 values (members
  silently missing). Now download_workers ecmwf 4 (was 12) and config archive.throttle (503/429/Slow Down: 3 more
  tries, 30/60/90 s), bound to the Herbie pullers by get_puller. Laptop test: ENS 18z 4800/4800 in 105 s, IFS 18z
  384/384 in 67 s with 4 workers (faster than 12: 157/173 s).

Archive (`archive/`)
- `forecasts.py`: one file per run; `run_points` (resort points + coarse models' 2x2 `model_cells`); `archive_run`
  (completeness check); `derive_run_fields` = THE derivation entry point: regimes -> modes -> add_snowfall.
- `runner.py`: an archive pass: PassLock, build_jobs (newest first; Canada 3-day catch-up), FailureLog
  (skip after 4 tries), run_jobs, split_streams/run_streams (one process per server), clear_caches.
  `scripts/archive_forecasts.py` is only its command line.
- `obs.py`: SNOTEL (UTC-8!), Synoptic, NOAA coop, soundings stores; `drop_unreliable_water`. Missed obs
  (2026-10-27): `with_retries` (config obs_fetch: 3 tries, 60 s apart; a failed source no longer stops the others)
  and `catchup_begin` (a scheduled fetch reaches back to the newest stored SNOTEL gauge hour of every site, max
  obs_fetch.catchup_max_days 30). Laptop network often fails right after wake (2 SNOTEL connection errors 10/27).

Derivation (pure functions, all tested offline)
- `profile.py`: profile at the station's real elevation, wet-bulb snow/mix/rain (NWS WR TA 19-01), dry-air nudge.
- `slr.py`: Kuchera. `regimes.py`: per-step 700 mb flow, storm phases, resort regime factors. Temperature: `band`
  (warm > -4 / mild > -10 / cold, config temp_bands_700mb_c) is what's shown and the finest factor level; the 2-way
  `thermal` (cold_700mb_c) is the factor fallback. factor_for: flow+band -> flow+thermal -> flow -> 1.0 (each needs
  factor_min_days). Archived runs are relabelled on read (with_bands), so changing band edges needs no re-archive.
- `modes.py`: precip mode per step (frontal / post-frontal / cellular / lake-effect).
- `gauge.py` (2026-10-27, owner approved): precip GAUGE running totals -> real hourly precip. Gauges wobble 0.1-0.2"
  with no precip; the old "sum every hourly rise" counted that (Atwater dry 10/27: 0.4"; months 30-300% high). Now
  only rises above the highest level so far count (config gauge: median_hours 1 = no smoothing, reset_drop_in 0.5 =
  gauge emptied). Used by storms.obs_steps (storm windows + observed water) and verify._window_change (gauge PREC
  sites, e.g. Collins). Matches each month's net gauge change within ~0.1" on the stored months.

Storms, verification, reports
- `storms.py`: storm life cycle - forecast -> verified, or dropped (future storm no newest run shows) / merged; splits
  noted ('split from'); per-model separate periods (fc_periods) and sub-periods (6 h gaps, fc_subperiods).
  Detect events (12 h dry gap), storm windows (LONGEST deterministic event), storm table, verify
  against SNOTEL once 12 h dry, scores. `bins.py`: the owner's snow bins (config verify.snow_bins_in, 0-2 added at
  the bottom 2026-10-27; lowest bin containing the amount) and the BIN SCORE (verify.bin_score: right bin +2, one bin
  off 0, -1 per bin further; signed bins_off, + = model too high). storms.bin_scores scores every deterministic run
  of each verified storm once it has observed snow (hand total / Collins readings, or 0 if SNOTEL dry; SNOTEL depth
  is never used) -> data/processed/bin_scores.csv, rebuilt every pass; the report's Verified section shows last run +
  runs >= 24/72 h ahead (verify.bin_score_leads_hours) and season totals. Not in the feed. `verify.py`: the separate DAILY-window verification (24 h windows ending
  12 UTC; NOAA ALTA at its 6 pm). Both are intentional: storms = event totals, verify = day-by-day skill.
- `report.py`: the daily model table, staleness warnings, and the storm report. Numbers and text are separate:
  `model_days` / `storm_models` compute, `model_table` / `render_storms` format; the feed reuses the numbers.
- POINTS (owner, 2026-10-28): 4 resort points (mid-mountain; feed + reports) + 2 INTERNAL SNOTEL-site points
  `solitude_snotel` (Mill-D North 8,940 ft) and `brighton_snotel` (Brighton SNOTEL 8,790 ft) at the gauges' own
  lat/lon/elev. Solitude and Brighton are SCORED ONLY at the SNOTEL points (mid-mountain points have obs: []);
  Alta/Snowbird unchanged. `internal: true` = archived, verified, in local reports, never in the feed
  (feed.public_points). New points exist only in runs archived after the change (coords are used at pull time).
  regime_factors.csv regenerated with factor_sites for them (identical to their resort's; old rows unchanged).
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
limits): ruff clean, vulture only the era5 item, no new long functions; nothing to fix. Fourth (2026-10-27 ~22:30,
after bin scores + obs retries): ruff/vulture clean; report.render_verified is 56 lines (split it with the HTML report
rework); fixed season bin totals vanishing when no storm was verified in the last 10 days. Fifth (2026-10-28, after
v1.2 + internal points): ruff/vulture clean, no new long functions; tidied feed.build_meta; offline suite measured
at ~12-20 s (the "~10 min" could not be reproduced: likely a pass running at the same time); its two slowest tests
sped up (real 2 s retry sleep -> injected sleep; nearest-index brute force 25 -> 8 points, grid built once).
GAUGE, NOT PILLOW (owner, 2026-10-28): verify.py scored SNOTEL sites by the snow PILLOW (WTEQ), which reads 0/blank
without snowpack and never sees rain (10/20-10/28: Mill-D North 4 WTEQ values vs 106 gauge, Atwater 42 vs 106).
Config verify.snotel_gauge_sites [atwater, mill_d_north] now scores those two on their gauge (PREC via gauge.py);
Snowbird and Brighton keep the pillow. Real data 10/15-10/28: Atwater 0 -> 63 scored pairs.

## Status (end of session 2026-10-27, late night)
Running live, TWO places for now (side-by-side check before cutover):
- LAPTOP: Windows tasks "Cottonwoods models" (every 3 h at :30 local, run_models.bat, DEVELOPMENT code = main) and
  "Cottonwoods soundings". Writes data/ locally. Never publishes (no CWX_PUBLISH), even on a release commit.
  Battery rules turned OFF 2026-10-27 (tasks start and keep running on battery): the 20:30 pass died when the charger
  was unplugged at 21:01 (StopIfGoingOnBatteries closed the headless console; later steps exit 0xC0000142).
  "Run after a missed start" stays on, so a pass runs when the laptop is opened after missing one.
- CLOUD: GitHub Actions github.com/CabbagePatchKim/cottonwoods-wx (PUBLIC), .github/workflows/pass.yml every 3 h at
  :45 UTC, runs the newest approved tag. Archive in Cloudflare R2 bucket; log in bucket
  data/logs/cloud/models_YYYY-MM.log. Publishes the feed (CWX_PUBLISH=1 set only there).
  First v1.0 publish: manual run 2026-10-28 03:13Z, green, 11 min, "pushed 3 files to cwx-feed". IFS 18z (not
  posted yet) and ENS 18z (S3 503 Slow Down, 624/4800 values) failed there; fixed in v1.1. NO scheduled run had
  fired yet as of 04:40Z 10/28 (both runs so far were manual).
- SCHEDULE UNRELIABLE (10/28): GitHub ran only 1 of 4 scheduled slots (08:45Z; 05:45/11:45/14:45Z dropped). Fix
  (owner approved 10/28): Cloudflare Worker cloud/trigger_worker.js presses "Run workflow" at :50 after each slot,
  skips if a run is queued/running/started in the last 30 min; GitHub's schedule stays as backup. Deployed by the
  owner via the dashboard (secret GITHUB_TOKEN = fine-grained, this repo only, Actions read+write). tests/
  test_cloud_trigger.py keeps its CRON in step with pass.yml. Check event "workflow_dispatch" runs at ~:50 appear.
- RELEASED v1.2 (owner approved 2026-10-28, tag on e9f923e, pushed 18:36Z): the IFS fix below. Laptop IFS 10/27 18z,
  10/28 00z/06z were uploaded to the bucket by hand (owner approved) so the cloud has no IFS gap. Check the first
  v1.2 pass log: "Running approved release v1.2", IFS archived.
- CLOUD IFS BROKEN since 10/27 (fixed in v1.2): CI installed Herbie 2026.10.0 (unpinned),
  which defaults ECMWF to the new 0.1 deg data (posted since 10/20, split into -sfc/-pl files) and fetches only
  -sfc; the pressure-level search returned an unwritten path -> FileNotFoundError on every IFS hour. Cloud ENS has
  been archived at 0.1 deg since 10/27 (laptop: 0.25). Fix: resolution "0p25" named in herbie_generic.SPECS,
  herbie-data pinned to 2026.9.2 in environment-ci.yml, field_files skips searches with no file. Both Herbie
  versions give identical IFS/ENS output (checked 10/28 06z). Switching to 0.1 deg = a separate owner decision.
- RELEASED v1.1 (owner approved 2026-10-27, tag on ece413a, pushed 04:38Z 10/28): lead limits, gauge-noise fix,
  bin scores, obs retries/catch-up, ECMWF throttle fix. The FEED still says v1.0 until the next cloud pass runs
  v1.1 (scheduled 06:45Z, or "Run workflow"). Friend heads-up: daily.json deterministic days 8-10 become null.
- FEED v1 (v1.0 approved 2026-10-27, v1.1 released 2026-10-27): MODEL-RUN DATA ONLY, public at
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
0. FIRST THING: report on BOTH passes before anything else.
   a. Laptop: passes since the 21:09 MDT manual pass (data/logs/models.log): all five steps exit 0? any "[obs] ...
      retrying" or "catching up" lines? GDPS/GEFS times (old averages GFS 756 s, GDPS 868 s, GEFS 405 s; 10/27 21:09
      pass: GEFS 359 s, ENS 157 s, IFS 173 s with 12 ECMWF workers; now 4).
   b. Cloud: did SCHEDULED runs fire on their own (api.github.com/repos/CabbagePatchKim/cottonwoods-wx/actions/
      workflows/pass.yml/runs, event "schedule")? As of 06:14Z 10/28 NONE had: the 02:45Z and 05:45Z slots
      were missing (all 4 runs so far were workflow_dispatch). If still no "schedule" run after the 08:45Z slot,
      the schedule is broken: investigate before anything else (gh isn't installed; use the API). First v1.1 run: log header "code v1.1", ENS/IFS complete (no 503),
      "pushed ... cwx-feed". Then meta.json code_version must be v1.1. Remind the owner to tell the friend.
1. Tonight's storm (10/27 18:00 - 10/28 06:00 MDT): should verify ~18:00 10/28 as DRY (SNOTEL 0.0" with the gauge
   fix) -> first bin scores (NAMNEST called ~2" regime-adjusted; HRRR/NAM/GFS/GDPS/RDPS 0). Check the storm report's
   Verified section and data/processed/bin_scores.csv.
2. Cutover once scheduled cloud runs are reliable for a day (owner approval): disable laptop tasks "Cottonwoods
   models" and "Cottonwoods soundings"; laptop becomes development only (pull from the bucket).
3. Weekend storm (Fri 10/30 - Mon 11/2; 10/27 runs: IFS 1.82" water / 16" Alta, GFS 1.29"/12", GDPS 0.80"/8", all
   ensemble members wet): first real verification ~11/2-11/3. The SessionStart hook will prompt the owner for Alta
   Collins readings (data/entries/, scripts/send_entries.bat); bin scores need them.
4. Owner's improvement list (2026-10-27; test speed DONE 10/28: suite ~12-20 s): speed up the offline test suite (~10 min, should be ~1 min:
   find the slow tests); store regime-adjusted snow in storm_scores too (score both); .gitattributes for line endings.
5. Then the build plan below (HTML storm report + timeline/trends + NOAA ALTA display: DEVELOPMENT ONLY, unpublished;
   feed v1 stays model-only until the owner approves a release that adds anything).

## Open questions for the owner
- (Resolved 2026-10-28: owner set Solitude = top of Apex lift 9,040 ft / 40.61369 -111.60199, Brighton = 9,514 ft /
  40.58766 -111.58009, replacing the ~9,200 / ~9,600 guesses. In main config; cloud needs a release, and archived
  runs keep the old elevation until re-derived - derived fields are computed at archive time, not on read.)
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
   Bin verification DONE 2026-10-27 (bins.py, bin_scores.csv). Still to do: bins in the forecast storm table.
5. Storm-day selector + backfill past storms.
5b. PATTERN archive (owner approved 2026-10-27; start early so a season accumulates before step 6): coarse regional
   fields for the long-range models (GFS, ECMWF HRES, GDPS + GEFS/ENS mean & spread): 500 mb height + vorticity,
   250 mb wind, MSLP, 850 mb temp, every 6 h, ~0.5-1 deg, box ~25-60N x 150-90W (upstream troughs to Plains).
   Few MB/run; server-side subsetting is the right tool here. Enables trough position/timing comparisons between
   models and runs (owner's 3-7 day "trend/convergence" method), pattern-based regime labels days ahead, and
   weighting models by pattern skill verified against analyses + SLC soundings. Point archive stays as is.
6. Learning layer last: per-model bias by lead/regime and by PATTERN (from 5b), weighted blend, calibrated ensembles, tuned pessimistic bin;
   the program's own frozen "CWX" forecast each cycle, scored like a model. Keep only what beats raw models.
   BIN SCORES GUIDE THE LEAN (owner, 2026-10-27): data/processed/bin_scores.csv (signed bins_off, points, lead, model,
   fc_flow/obs_flow) is the record CWX uses to choose its bin - which models to trust and which way (up/down a bin)
   to lean, by lead and regime. Train only on storms verified before the forecast was issued.
FEED v1 ADDITIONS (owner, 2026-10-28: noted, implement LATER, with the HTML storm report - same numbers): the live
   storms.json numbers parse cleanly, but flow/modes/parts/notes are display strings (regex to restyle), there is no
   per-storm consensus, no storm status, no trend. Sketch: docs/feed_v1_additions_sketch.jsonc (status, summary per
   point: bin/median/range/models counted/ensemble wet fraction/confidence/change; structured phases; per-model
   `previous`; ensemble water percentiles). All additive (v1 rules), keep the text fields; ship as v1.3 on owner
   approval. Four open owner decisions are listed at the end of the sketch.
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
