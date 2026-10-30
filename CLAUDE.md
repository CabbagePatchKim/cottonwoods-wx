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
  BLENDED + UNMODIFIED, STORED APART (owner 2026-10-29): for every blended model (cells.blends: GFS, IFS, ENS, GDPS,
  RDPS) archive_run also writes data/archive/forecasts_raw/model=X/init=Y = the same pull, resort points only at
  their nearest grid point, derived with blend=False (raw_run). data/archive/forecasts stays BLENDED: the reports
  and the feed read only it (owner: feed keeps the blend). load_forecasts(raw=True) labels them "GFS raw"
  (RAW_LABEL); storms.run(fc_raw=) and scripts/verify.py SCORE them beside the blended runs (storm_scores,
  block_scores, bin_scores, verification_pairs rows "GFS raw"); never used to find or time storms; report sorts
  "X raw" right after X (model_rank). fill.py fills the raw copy from the same pull; rederive covers both stores;
  bucket.RUN_FILE knows forecasts_raw. FROM 2026-10-29 ON ONLY (owner): older runs have no raw copy (their resort
  rows were overwritten by the blend; e.g. Brighton's GDPS nearest point isn't in the block, so unrecoverable).
- `runner.py`: an archive pass: PassLock, build_jobs (newest first; Canada 3-day catch-up), FailureLog
  (skip after 4 tries), run_jobs, split_streams/run_streams (one process per server), clear_caches.
  `scripts/archive_forecasts.py` is only its command line.
- `obs.py`: SNOTEL (UTC-8!), Synoptic, NOAA coop, soundings stores; `drop_unreliable_water`. Missed obs
  (2026-10-27): `with_retries` (config obs_fetch: 3 tries, 60 s apart; a failed source no longer stops the others)
  and `catchup_begin` (a scheduled fetch reaches back to the newest stored SNOTEL gauge hour of every site, max
  obs_fetch.catchup_max_days 30). Laptop network often fails right after wake (2 SNOTEL connection errors 10/27).
  NOAA COOP (ALTA) FIX (2026-10-29, in main, not released): every cloud pass logged "[obs:ghcn] ... 0 rows": NOAA
  posts ALTA 1-3 weeks late (10/29: newest day 10/22) and the pass asked only the trailing ~4 days. Now
  obs.ghcn_begin: a scheduled fetch re-asks the last obs_fetch.ghcn_days (35). Real check: 87 rows 9/24-10/22.

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
- Tests never import pytest: CI (tests.yml) runs tests/run_offline.py in environment-ci.yml, which has no pytest.
  Use `abs(a - b) < 1e-9` and try/except + AssertionError. 10/29: four new files imported pytest and CI failed on
  every commit from 6a01f7f to 5e9b983 unnoticed. After every push, CHECK the tests.yml run (api.github.com/repos/
  CabbagePatchKim/cottonwoods-wx/actions/workflows/tests.yml/runs); never tag a commit whose CI isn't green.
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
Sixth (2026-10-29, after v1.6 + freezing scores): ruff clean; vulture: era5 item, patterns.load_pattern and
patterns.FIELDS: all kept on purpose for planned work (owner: never delete code planned for future development;
FIELDS was removed, then restored). Treat these vulture hits as expected. Split report.render_verified (81 ->
25 + _verified_storm 43 + _season_sections 23) and report.storm_block_lines (63 -> 49 + _rest_of_storm,
_observed_blocks); freezing "wet" window moved from code to config verify.freezing_wet_hours. Golden IDENTICAL.
Still just over 60 (cohesive): profile.station_profile 68, gfs._pull_run_aws 68, ghcn.fetch 62, regimes.phases 62,
herbie_generic.pull_ensemble 60.
Seventh (2026-10-30, pre-v1.7 review, owner: "very thorough: spaghetti code, accuracy errors, misleading lines"):
FIXED: ensemble runs shorter than the storm (RRFSENS 60 h) were counted in the summary's "ensembles wet" and the
ensemble trend/changes sentence unflagged -> now left out of the wet share, flagged "(partial)", compared only
full-to-full (trends.ensemble_trends full); summary note now defines bin / models / since last run / ensembles wet /
confidence with config thresholds (htmlreport.summary_note); NOAA storm totals say they are whole observer days;
lagged header "members / runs"; "owner's bin" -> "your snow bin" on the page; report._snow_col (used by 4 modules)
-> public report.snow_col; probs.family_ratios param no longer shadows it; registry.models_of (test-only) removed;
the Utah Snow Ensemble was archived but NOT SHOWN -> htmlreport.use_html + uu.use_over (storm-window percentiles,
labelled APPROXIMATE: difference of since-init percentiles). Vulture hits now expected: era5, patterns FIELDS /
load_pattern (planned), uu._Tables.handle_* (HTMLParser callbacks), uu.load_use (scripts/storms.py).
OBSERVED WATER STANDARD, every site (owner, 2026-10-28): the precip GAUGE is primary (SNOTEL + Synoptic PREC via
gauge.py, NOAA PRCP); a SNOTEL snow PILLOW (WTEQ) is the fallback (gauge missing AND >= min_snowpack_in on the pillow)
and the cross-check. verify.observed_water is the one place for this rule; pairs carry gauge_obs_in, pillow_obs_in,
snowpack_in, undercatch_flag (pillow rose >= min_rise_in and > undercatch_ratio x gauge; config verify.pillow_check).
UNDERCATCH EVIDENCE (no correction yet): verify.gauge_pillow_days -> data/processed/gauge_pillow_days.csv (every
SNOTEL site x 24 h window, with balloon flow via regimes.balloon_regime; scripts/storms.py recomputes the last 25 days
each pass ~8 s, the whole archive the first time ~32 s: balloons only from the first SNOTEL day) and undercatch_summary.csv (site x flow). First look 10/28 (13-17
snowpack days per site): pillow/gauge Mill-D North 1.46, Snowbird 1.24, Brighton 1.17, Atwater 1.06. Fit a
correction only after a season of evidence, verified like the regime factors.
REPORT LAYOUT (owner, 2026-10-28): daily table and storm tables are fixed-width: a block per day/point under its
label with two numbers named on a sub-header row (water snow / wet/N mean), run = cycle only (timefmt.cycle), legend
at the top. report._cells is the one cell formatter; storm_models carries "ensemble".

## Status (end of session 2026-10-28, ~19:45Z / 13:45 MDT)
Running live, TWO places for now (side-by-side check before cutover):
- LAPTOP: Windows tasks "Cottonwoods models" (every 3 h at :30 local, run_models.bat, DEVELOPMENT code = main) and
  "Cottonwoods soundings". Writes data/ locally. Never publishes (no CWX_PUBLISH), even on a release commit.
  Battery rules turned OFF 2026-10-27 (tasks start and keep running on battery): the 20:30 pass died when the charger
  was unplugged at 21:01 (StopIfGoingOnBatteries closed the headless console; later steps exit 0xC0000142).
  "Run after a missed start" stays on, so a pass runs when the laptop is opened after missing one.
- CLOUD: GitHub Actions github.com/CabbagePatchKim/cottonwoods-wx (PUBLIC), .github/workflows/pass.yml every 3 h at
  :45 UTC, runs the newest approved tag. Archive in Cloudflare R2 bucket; log in bucket
  data/logs/cloud/models_YYYY-MM.log. Publishes the feed (CWX_PUBLISH=1 set only there).
  Runs so far: #1-4 manual (10/27-28), #5 schedule 08:58Z (first v1.1, green), #6 schedule 17:23Z (16:45 slot,
  38 min late, v1.1, green). Both v1.1 runs failed IFS (Herbie 2026.10 bug, fixed in v1.2/v1.3). No pass has run
  v1.2; the next one (20:45Z slot or Worker 20:50Z) runs v1.3 directly.
- WORKER IS THE ONLY TRIGGER (owner 2026-10-29, live from main): pass.yml has no `schedule:` and no gate job any
  more; the Cloudflare Worker dispatches at :50 after 02/05/.../23Z. Why: over 10/29 GitHub's schedule dropped ~half
  its slots and ran the rest 0 min to 2 h 48 min late; a late run > 40 min after the Worker's (22:00Z) was a wasted
  extra pass. The Worker itself is unchanged (its repo copy's comments updated; no redeploy needed). If the Worker
  fails, no pass runs: check its Logs tab, or press "Run workflow" by hand.
- (history) SCHEDULE UNRELIABLE (10/28): GitHub ran only 1 of 4 scheduled slots (08:45Z; 05:45/11:45/14:45Z dropped). Fix
  (owner approved 10/28): Cloudflare Worker cloud/trigger_worker.js presses "Run workflow" at :50 after each slot,
  skips if a run is queued/running/started in the last 30 min; GitHub's schedule stays as backup. Deployed by the
  owner via the dashboard (secret GITHUB_TOKEN = fine-grained, this repo only, Actions read+write). tests/
  test_cloud_trigger.py keeps its CRON in step with pass.yml. Check event "workflow_dispatch" runs at ~:50 appear.
- RELEASED v1.6 (owner approved 2026-10-29, tag on 8436a0e, CI green): NOAA coop (ALTA) fetch window, blended +
  unmodified stores both scored (forecasts_raw), RDPS blended on its own grid. Manual "Changes by version" has v1.6
  (RDPS blend: feed-visible). CHECK the first v1.6 pass: "Running approved release v1.6", obs:ghcn > 0 rows,
  data/archive/forecasts_raw/ files for GFS/IFS/ENS/GDPS/RDPS, RDPS main files with rdps_* cells, feed code_version.
- RELEASED v1.5 (owner 2026-10-29 "make the change then publish 1.5", tag on 7860c9e, pushed ~07:20Z, CI green;
  bucket backup of every forecast run + archive CSVs at backup/pre-v1.5/ (134 objects, outside the synced data/
  prefix). Rollback: delete the v1.5 tag (the next pass runs v1.4) and copy backup/pre-v1.5/data/... back. First
  v1.5 pass expected 08:45Z slot (also the gate's first real test): check "Running approved release v1.5", the
  rederive + patterns + obs:rap steps, feed meta code_version v1.5, README.md on cwx-feed. Contents: everything
  listed as "in main" below.
- (was IN MAIN, now in v1.5; owner 2026-10-28: bundle with the next change, no v1.5 just for this): 82effa7 storm
  report matches the feed (ended storms = one "ENDED: awaiting verification" line; runs starting after the storm
  began show "-", not 0.00). render_storms now takes `now`. Cloud report keeps the old layout until the next release.
  Also in main, same bundle: report labels for the internal SNOTEL points (points.*.label in stations.yaml):
  solitude_snotel shows as "Mill-D North SNOTEL" / "Mill-D SNTL" (owner: "solitude SNTL" was misleading; the gauge
  is ~5.7 km down-canyon of the resort), brighton_snotel as "Brighton SNOTEL". Keys unchanged (archive, verification).
  Also in main, same bundle: HOLE-FILLING (owner 2026-10-28, "with accurate data"): archive/fill.py +
  runner.fill_holes, run by every scheduled pass after its new runs (config archive.fill: runs <= 72 h old, 2 per
  stream per pass, 3 tries each, data/archive/fills*.csv). Re-pulls the WHOLE run (precip is cumulative/buckets),
  refuses if ANY archived value differs from the new pull (FillMismatch), fills only empty values and missing steps
  (a merged step is split only if the new steps add up), re-derives, keeps fetched_at, adds filled_at /
  filled_values, manifest note. Found by comparing laptop vs cloud archives 10/29: 29/37 runs identical, the rest
  single timed-out files. Real-data check 10/29: HRRR 18z, RDPS 18z, GDPS 12z filled and matched the other
  machine's independent copy value for value (GDPS resort rows differ only by the blend, re-derived). RDPS
  06z/12z 10/28 correctly REFUSED: archived before the Solitude/Brighton move, so grid_dist differs. DERIVED list
  moved to archive/forecasts.py (golden.py imports it; part of agenda 3b).
  Also in main, same bundle: 12 H BLOCKS in the storm report (owner 2026-10-29; blocks.py +
  report.storm_block_lines, config storms.block_*). Why: the weekend storm merged into one 75 h window, longer than
  HRRR (48 h) / NAM Nest (60 h) reach, and runs starting mid-storm show "-", so short-range models never counted.
  Model-defined parts were rejected (owner: snow can fall in part of a part; parts follow one model's timing).
  Fixed blocks ending 00z/12z, clipped to the storm (they add up to its total); each run fills the blocks it
  reaches, "*" = part of a block. Kept small (owner: "keep the report from getting too busy"): ONE table of
  block_point (Alta) snow by model + median + ensemble % members wet, ONE table of every point's median.
  Under way (now inside the storm): an "observed" row (block_point's SNOTEL WATER per block so far, "*" = block
  still going; water, not snow: SNOTEL depth is too noisy) and a "rest" column (each model's snow from now to the end).
  Also in main, same bundle: SCORING (owner 2026-10-29, "keep all the scoring internal"; feed.NOT_MODEL_DATA already
  refuses score/obs keys). Why: NAM Nest's 1.4" (and 4.1" 12 h out) for the DRY 10/27 storm scored +2 like HRRR's 0.
  (A) event call: a false alarm / miss (>= event_swe_in water) scores at most verify.bin_score.event_cap (0) even in
  the right bin; bin_points keeps the uncapped score; report marks FA / miss. (B) snow_err_in / water_err_in in
  bin_scores. (C) every deterministic run within verify.score_runs_hours (72) of the storm, averaged (per storm table
  + season "all runs"). (D) every point with a SNOTEL gauge is scored (storms.point_steps; storm_scores has a row
  per point; Snowbird, Mill-D North, Brighton SNOTEL besides Alta; observed snow there = hand entry or 0 if its
  gauge was dry); storms verified earlier get the new points added (_score_new_points) while their runs are loaded.
  BLOCK SCORES: data/archive/block_scores.csv (storms.score_blocks): each deterministic run on every 12 h block it
  covered, issued before the block began, vs that block's SNOTEL water, every gauged point; report columns
  "12h blocks / block water off / block FA/miss" (runs within 72 h of each block). Real 10/27 re-score: NAM Nest
  last run +2 -> +0 FA, avg +0.4 over 8 runs, 10 FA; IFS 24 h run FA; HRRR/RDPS/GFS/GDPS +2.
  LEAD RANGES (owner: "more fair"): the season table averages every deterministic run by lead before the storm
  (verify.lead_bands_hours: 0-24 h, 1-3 d, 3-7 d; report.bin_totals), so models are compared only at the same
  distance and long-range calls get credit. Run counts differ by design: HRRR/NAM Nest ranges stop short, GDPS runs
  2x/day; for 10/27 GFS/IFS also had fewer because their archive starts 10/25 12z (RDPS backfilled to 10/24 18z).
  NOT BUILT: block snow bins (no observed snow per block: Collins boards could give Alta's); feed: never for scores.
  WIND BUG FIXED (found 2026-10-29 building regimes): HRRR / NAM Nest / NAM (Lambert) and RDPS (rotated lat-lon)
  store GRID-relative u/v (GRIB uvRelativeToGrid=1); never turned, so their 700 mb directions read +16/+7/+12/+18 deg
  clockwise vs GFS/IFS and flow labels were off (the 10/27 storm's defining NAM Nest run said "W 89%"; RAP + the
  turned run say SW). winds.py (grid geometry; angles at Alta 8.80 / 7.03 / 16.90 deg = real files) turns them as
  derive step 0, once (uv_earth). DERIVE_VERSION 2 + scripts/rederive_archive.py (agenda 3b, now BUILT) re-derive
  older runs (winds + blend + elevations); laptop archive done 10/29 (122 runs, 91 s; backup of v1 runs in the
  session scratchpad); every pass (cloud step "Re-derive older runs", skipped by releases without the script; laptop
  run_models.bat) runs it, so the bucket's runs get re-derived by the first pass of the next release. After it, all
  models are within +-5 deg of GFS/IFS. fill.py turns a fresh pull the same way before comparing.
  OBSERVED REGIME (owner 2026-10-29): hourly RAP analyses (obs/rap.py, archive obs/rap, config rap; fetched by
  archive_obs.py, only new hours; backfilled locally from 10/25) at each point, weighted by observed SNOTEL precip
  (equal weights for a dry storm), = storms.obs_flow (obs_flow_source rap); SLC balloons (also precip-weighted) kept in
  obs_flow_balloon, report flags "balloons disagree". Owner: group by the STORM's regime, never each model's own flow
  (a dry run's flow would make a regime look dry). Report: per storm "flow:" line; season tables by the storm's
  FORECAST regime (defining run's dominant phase) and 12 h blocks by OBSERVED block regime (block_scores fc_regime /
  obs_regime). Verified storms get relabeled + fc_flow recomputed once RAP covers them (_backfill_verified).
  DOUBLE-RUN GATE (owner 2026-10-29; REMOVED later that day with GitHub's schedule): pass.yml job "gate" ends a SCHEDULED run created < 40 min after another pass
  (GitHub's late schedule after the Worker's dispatch, 02:50/02:51Z 10/29); manual/Worker runs always run; fails open
  (API error -> the pass runs). LIVE NOW: workflow files run from main, independent of the release tag.
  FULL TEST OF EVERYTHING SINCE v1.4 (owner 2026-10-29, at 67e3a75+): 135 offline tests + ruff; then the BUCKET pulled
  into a scratch data folder (COTTONWOODS_DATA) and every pass step run with main's code: fills (RDPS 10/29 00z 48,
  10/28 18z 24; NAMNEST 10/27 18z + ENS 10/26 12z correctly refused: moved points / old 0.4 deg grid), rederive 124
  runs (winds within +-6 deg of GFS/IFS after, 6-19 before; a step missing one wind component loses both: by design),
  patterns 20 runs / 71 MB first catch-up (~12 min; later passes ~2 min), RAP 77 h, storms (blocks, labels, RAP flow,
  balloons-disagree flag, FA, lead ranges, regime tables), model report, feed preview: SAME KEYS as live v1.4 in
  meta/storms/daily, no score/obs keys. FOUND + FIXED: (1) BUCKET RACE: pass #13 (05:50Z) pulled before the owner's
  Collins entry was pushed and its final push at 06:27Z overwrote it with its stale copy. bucket.push now keeps any
  bucket file changed after the local copy was written (an explicit --only push, e.g. send_entries.bat, still
  forces). Entry re-sent 07:01Z. UNTIL THE NEXT RELEASE the cloud still runs v1.4's push: send entries only when no
  pass is running (passes ~:45-:15 after 02/05/08/11/14/17/20/23Z). (2) RAP's Herbie cache went to ~/data; now
  data/raw/herbie_rap, cleared after each fetch.
- RELEASED v1.4 (owner approved 2026-10-28, tag on 5827cd1, pushed ~22:17Z, CI tests green; first pass #9 22:28Z
  manual by the owner: "code v1.4", all steps success, feed v1.4 published, no new runs that pass so the feed still
  showed pre-release nearest-cell 12z numbers; blended numbers arrive with each model's next run). Contents:
  (a) archive/cells.py: coarse models (model_cells) give each point the BILINEAR blend of its 2x2 cells (step 0 of
  derive_run_fields; cell rows untouched; idempotent). Why: nearest-cell put Solitude (Apex) and Mill-D North in
  cells 28 km apart, so lower sites got more snow (GFS 12z 10/28: Mill-D 10.3" vs Solitude 7.0"). Archived runs keep
  nearest-cell values until re-derived (rederive_archive.py, agenda 3b, applies it). (b) storm_models: a run that
  starts after the storm began is not "full" (past storms showed 0.00" from later runs). (c) config feed.regime_snow
  false: feed sends snow_in_regime null, snow_regime_adjusted false (key kept: v1-safe); reports keep regime snow.
  (d) feed.build_storms(now): a storm leaves the FEED once fc_end passes (owner chose this 10/28 over an
  "all deterministic 0" drop rule: the 10/28 dry storm only looked all-zero because every newest run started after
  it; NAMNEST 00z had 0.23"); it stays "forecast" in storms.csv until verified, local reports unchanged.
  (e) GEFS model_cells `blend: false` (0.5 deg block reaches the Salt Lake Valley): GEFS keeps its nearest cell.
  Friend heads-up needed: snow_in_regime becomes null; storms leave when their window ends. Manual updated.
- RELEASED v1.3 (owner approved 2026-10-28, tag on 398d465, pushed ~19:31Z, CI tests green): Solitude 9,040 / Brighton 9,514 ft; internal SNOTEL points
  solitude_snotel + brighton_snotel (scored there only); gauge-first observed water + undercatch evidence; fixed-width
  report tables. Feed format unchanged. regime_factors.csv (rows for the new points) uploaded to the bucket 10/28.
  Still to do: scripts/rederive_archive.py so archived runs get the new Solitude/Brighton elevations.
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

## Next session's agenda (owner: come back after the cloud has run v1.3 a couple of times)
0. FIRST THING (session ended 2026-10-29 ~07:25Z, right after tagging v1.5): check v1.5's first passes in the bucket log
   (data/logs/cloud/models_2026-10.log) + api.github.com .../pass.yml/runs: "Running approved release v1.5"; steps
   models, rederive ("re-derived ~130 runs to version 2" once, then 0), patterns (~20 runs first time), obs (obs:rap),
   storms, report, feed all success; feed meta code_version v1.5; README.md on cwx-feed = the v1.5 manual; the gate
   (a scheduled run < 40 min after another pass shows job "pass" skipped; never both skipped). Then the weekend storm
   (starts Fri 10/30 09:00 MDT): blocks + "observed so far" in data/reports/storms_latest.txt; verifies ~11/2-11/3 ->
   Collins entries, first long-storm block scores. Older notes for this item below.
0. (older) FIRST THING: report on BOTH passes before anything else.
   a. Cloud (bucket log data/logs/cloud/models_2026-10.log + api.github.com/repos/CabbagePatchKim/cottonwoods-wx/
      actions/workflows/pass.yml/runs): first v1.3 pass shows "Running approved release v1.3"; IFS archived again
      (10/28 12z onward, no FileNotFoundError); ENS back on 0.25 deg; solitude_snotel/brighton_snotel rows in new
      runs; first storms.py run built gauge_pillow_days.csv (~32 s); "pushed 3 files to cwx-feed"; meta.json
      code_version v1.3, still 4 points. SCHEDULE: count slots that ran by "schedule" vs the Worker's
      "workflow_dispatch" (~:50); any slot with neither = Worker problem (owner checks its Logs tab in Cloudflare;
      a 401/403 = token). Ask the owner what the Worker log showed at 17:50Z 10/28 (expected "skip: run 6").
   b. Laptop (data/logs/models.log): it slept 00:28-08:28 and 08:28-11:03 MDT 10/28, so passes only run while it's
      awake; SNOTEL DNS failures right after wake are expected (retries + catch-up cover them).
1. Storm 2026102700 (10/27 18:00 - 10/28 06:00 MDT) should verify as DRY ~18:00 10/28 -> first bin scores
   (data/processed/bin_scores.csv, storm report Verified section).
2. DONE 2026-10-29 (owner): laptop tasks "Cottonwoods models" and "Cottonwoods soundings" DISABLED (Task Scheduler;
   re-enable there if ever needed). The cloud runs every pass; laptop = development only (bucket_sync.py pull). The
   laptop's archive was re-derived (v2) and has RAP + pattern files the bucket lacks; a pull keeps newer local files,
   so for a clean mirror delete data/archive/forecasts locally and pull.
3. Weekend storm (now split: 10/30 12:00 - 10/31 06:00 and 10/31 18:00 - 11/02 17:00 MDT in the 10/28 runs): first
   real verification ~11/2-11/3. SessionStart hook prompts for Alta Collins readings; bin scores need them.
3b. scripts/rederive_archive.py (owner agreed 10/28, not built): strip the derived columns (move tools/golden.py's
   DERIVED list into archive/forecasts.py, one place), re-run derive_run_fields with the current config, rewrite.
   Then laptop: pull bucket, re-derive, push back (AFTER v1.3 is live, so no pass adds old-elevation runs between).
   Gives archived runs the new Solitude/Brighton elevations. Offline test + before/after check.
4. Owner's improvement list (2026-10-27; test speed DONE 10/28: suite ~12-20 s): speed up the offline test suite (~10 min, should be ~1 min:
   find the slow tests); store regime-adjusted snow in storm_scores too (score both); .gitattributes for line endings.
   DONE in main (owner 2026-10-29: "publish the updated README with each new published version"; the friend reads
   the repo, no separate report): every approved publish copies that release's docs/feed_v1_user_manual.md to
   cwx-feed as README.md (github_feed.publish readme=). Starts with the next release (v1.4 lacks the code). The
   manual has a "Changes by version" list: ADD each release's feed-visible changes there before tagging it (its
   "Next version" entry becomes that tag's). Keep the manual in step with feed.py whenever the feed gains fields.
5. Then the build plan below (HTML storm report + timeline/trends + NOAA ALTA display: DEVELOPMENT ONLY, unpublished;
   feed v1 stays model-only until the owner approves a release that adds anything).

## Open questions for the owner
- (Resolved 2026-10-28: owner set Solitude = top of Apex lift 9,040 ft / 40.61369 -111.60199, Brighton = 9,514 ft /
  40.58766 -111.58009, replacing the ~9,200 / ~9,600 guesses. In main config; cloud needs a release, and archived
  runs keep the old elevation until re-derived - derived fields are computed at archive time, not on read.)
- (Resolved 2026-10-29: journal actuals are the ALTA STAKE, actual_site alta_stake. Added 2/16-2/18 cycle 46 in, on
  2026-02-16a, whose forecasts cover the whole cycle; 3/5 16 in. Journal by model, last forecast per storm: ECMWF
  1.5" avg error (3 storms), HRRR 3.8 (4), GDPS 6.1 (6), RDPS 10.4 (7, bias -2.5: big misses both ways). OWNER on
  GDPS (2026-10-29): steadiest in the journal, but its coarse grid makes the freezing level and local influences
  hard to catch in a marginal storm like the 10/30-11/2 one; weigh that when its snow split is compared.)
- PASS STEP OUTPUT (owner 2026-10-30): OBSERVATIONS step = two tables (archive_obs.py + obs.latest_obs): each source's
  status / newest (SNOTEL hour, NOAA ALTA observer day, RAP hour, Synoptic skipped) and each SNOTEL site's latest
  (gauge water 6 h / 24 h via gauge.py, depth, pillow, temp); failures still print in full. Herbie's "pattern is
  interpreted as a regular expression" UserWarning is silenced in models/fields.py (~1,300 lines per pass). NEW
  "Verified" STEP after Model report (pass.yml, LIVE from main): storms.py writes verification (newly verified,
  render_verified, freezing levels) to data/reports/verified_latest.txt instead of printing it; the step cats it if
  written in the last 90 min, else says the release prints it under Storms (v1.6 does). HTML full text = both.
- MODEL KINDS + RRFS + U OF U (owner 2026-10-30, in main, not released). THREE KINDS NEVER MIX (owner: "otherwise the
  report is going to get messy"): registry ModelSpec.kind -> kind_of(model) / models_of / deterministic(fc).
  deterministic = storm windows (storms.model_events), summary medians/bins, timelines (trends), main tables;
  guidance = its own table (text storm table group "guidance", HTML "Guidance at Alta": theirs vs OUR newest run of
  ModelSpec.based_on, plain snow), scored like a model, never in medians or windows; ensemble = chances + one line.
  model_days (daily table) leaves guidance out. FEED ALLOWLIST config feed.models (feed.build_feed filters runs AND
  staleness warnings): owner 2026-10-30 ADDED RRFS for v1.7 (manual entry v1.7 + model table); RRFSENS stays internal
  (5 members, 60 h); UU guidance NEVER (U of U's experimental product, not ours to republish).
  NAM / NAM Nest "remove on 11/3, keep archives" (owner): registry.retired_by(model, cfg, when) drops them at
  archive.retired from the feed (at the pass time), the high-res lagged set (storms starting after it,
  probs._lagged_models) and the season verification tables + freezing table (report._active); per-storm verified
  sections still show them for storms they forecast (the weekend storm: the one NAM Nest vs RRFS comparison).
  SCN 26-47 Updated (Oct 2): termination Nov 3 12z, slips to the next weekday if a Critical Weather Day (owner had
  seen an older 10/14 date). UUGFS only_cycles 00/06/12 (their 18z file exists ~13 days/month); UU parsers accept
  "0:1/-" (ratio 0). FIRST v1.7 PASS: the rrfs stream catches up 8 cycles x ~7 min (~1 h); later passes ~7 min.
  RRFS (models/rrfs.py, Herbie model="rrfs", AWS noaa-rrfs-ops-pds; operational 2026-11-03, replacing NAM/NAMNEST):
  RRFS deterministic 3 km, hourly to 48 h then 3-hourly to 84 h, products 2dfld + prslev; RRFSENS = members m001-m005
  (2dfldnomads + prslevnomads: no 650/550 mb columns), 3-hourly to 60 h, WITH temperatures, so each member gets its
  own snow (probs.ensemble_probs own_snow; modes.add_precip_modes now groups by point AND member). APCP and RRFS's
  own ASNOW read as totals since init and deaccumulated; ASNOW kept as snow_native_in (m -> in) to compare with ours.
  Stream "rrfs" (download_workers.rrfs 8). Real pull 10/30 00z: 6 hours ~70 s (full run est. 8-12 min).
  RETIREMENT: config archive.retired {NAM, NAMNEST: 2026-11-03T12:00} (registry.retired_from): recent_cycles /
  cycles_between stop at that cycle, so no new jobs and no staleness warnings; archived runs stay scored. The SCN
  date moved several times: correct it in config if it moves again. probs.lagged models now include RRFS.
  U OF U GUIDANCE (models/uu.py, config uu; experimental/unofficial, credit the Steenburgh group, internal only):
  UUHRRR (1-48 h hourly) and UUGFS (1-120 h hourly, 123-180 3-hourly) at Collins = point alta only (ModelSpec.points);
  text files per run back to 2026-01 (~115/month each). Their totals since init deaccumulated; snow kept as
  snow_issued_in (derive_run_fields sets snow_in from it when a run has no profile), slr_issued, wbz_msl_m (NaN when
  printed "≤ KSLC" (GFS) or "<KSLC" (HRRR): below the SLC airport). Stream "uu". UTAH SNOW ENSEMBLE (82 members,
  percentiles min/10/25/50/75/90/max every 6 h to 10 d at the CLN grid point; only the newest run is posted):
  uu.archive_use saves each new run to data/archive/uu_ensemble/init=*.csv, called by archive_forecasts.py's uu stream
  each scheduled pass. RRFS SNOW ENSEMBLE page (weather.utah.edu rrfsqsf) is IMAGES ONLY: not archived (link only).
  JOURNAL SCORING: journal.storm_window (start date 0000 MT -> 0600 MT after end date; journal has dates only) +
  journal.score_runs + scripts/score_journal_runs.py (laptop) -> data/local/journal_run_scores.csv. Backfilled UU runs
  around journal storms locally (--journal; 2023-24 storms not on their server). FIRST RESULT, UUGFS last run vs Alta
  stake: 2/10 12.9" vs 11.5 (+2), 2/16-18 40.6" vs 46 (0, one bin), 3/5 18.9" vs 16 (+2); journal's raw GFS had 30-36"
  for 2/16-18. UUHRRR (48 h) never covers a whole multi-day journal storm.
- HTML STORM REPORT (owner 2026-10-29, in main, not released; internal, never the feed): scripts/storms.py writes
  data/reports/storms_latest.html next to the text (htmlreport.render_html: one self-contained file, inline CSS, no
  scripts, light/dark). Per open storm: per-resort summary (owner's bin of the median of full deterministic runs,
  median, range, median change vs each model's previous run, ensemble wet share, confidence from config
  html.confidence), a "changes since last run" sentence, CHANCES (probs.py, UNCALIBRATED shares of members: GEFS/ENS
  storm water >= probs.water_in, snow bins with members' water x the family deterministic run's snow per inch in each
  12 h block (probs.families GEFS->GFS, ENS->IFS; owner 2026-10-30: kept, with a page note that these bins
  reflect water spread only, every member sharing that run's temperatures); the time-lagged high-res set (probs.lagged: HRRR/NAMNEST/NAM/RDPS
  last 4 runs) for the whole storm AND block by block, since short-range runs rarely cover a long storm), TIMELINE
  (trends.py at block_point: onset / peak / end from 3 h snow rates, config trends; change vs previous run; last 4
  runs' snow + direction; ensembles wet/mean by run); then NOAA ALTA daily PRCP/SNOW/SNWD (observer day ending 6 pm
  MST) + storm totals (days posted of days needed), and the full text report collapsed. Laptop preview 10/29 data:
  ENS 88% >= 1" water at Alta, likeliest 4-8 (38%); GFS 7 -> 9 -> 12 -> 15" up. Render ~25 s per pass.
- REGIME SCORES, LOCAL ONLY (owner 2026-10-29: no release, laptop only): scripts/regime_scores.py, run after
  `bucket_sync.py pull`: storms.regime_bin_scores rescored every verified storm by the pass's own rules with plain and
  regime-adjusted snow (regimes.apply_factors, today's regime_factors.csv) -> data/local/regime_bin_scores.csv
  (data/local: not synced, not in git) + report.regime_score_table (plain vs regime score / snow miss, by lead).
  Rescore a storm within ~3 weeks of verifying (pull keeps 25 days of runs). First run: only the dry 10/27 storm,
  plain = regime. The weekend storm (verifies ~11/2) is the first real test: run it by ~11/20.
- FREEZING-LEVEL SCORES (owner 2026-10-29, in main, not released): verify.freezing_level_scores: every deterministic
  run (blended + "X raw") at storms.point's model column vs each SLC balloon (00z/12z) a step ends on; both = highest
  0 C crossing MSL. wet = >= event_swe_in observed at the point in the 12 h around the balloon (the marginal-storm
  cases). data/processed/freezing_scores.csv (merged each pass, so scores outlive the 20-day run window); storm
  report section "Freezing level vs SLC balloons" (report.freezing_table: bias ft / miss ft / n by lead band, all
  and wet). First look (laptop data to 10/29 00z, 8 dry balloons): every model runs warm vs SLC by ~100-340 ft at
  0-24 h; misses 250-620 ft. Caveat: Alta's column vs a balloon ~35 km away. RAP stores only t_700 (no RAP freezing
  level yet).
- Confirm storm window start/end times in `data/journal/storms.csv` (`window_confirmed=false` rows).
- (Resolved 2026-10-26: the 11/23, 12/1, 12/8 and 4/15 storms are the 2023-24 season.)

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
5a. PRISM WATER RATIO, INTERNAL ONLY (owner, 2026-10-28; never in the feed unless a later release approves it):
   coarse models only (GFS, IFS, GDPS, ENS; not HRRR/NAMNEST at 3 km): scale each point's model water by
   PRISM 800 m normals PRISM(point) / PRISM(blended cell area), shown next to raw in the local reports and scored
   like a model. Why: after the cell blend (v1.4) temperature already follows each point's real elevation; the
   remaining differences between resorts are water (coarse QPF barely separates Alta from Solitude), and regime
   factors borrow Mill-D North for Solitude. A DEM-average elevation per sub-cell was considered and rejected:
   points already use surveyed elevations. Start after a handful of verified storms (first: Sun 11/1 storm,
   verifies ~11/2-11/3); keep only if it beats raw water on verified storms (bin scores + water error).
5a2. RDPS CELL BLEND: BUILT 2026-10-29 (owner: "keep the blended", then build it with the separate stores; in main,
   not released). Why: RDPS (10 km rotated grid) gave alta/snowbird/solitude/brighton/brighton_snotel ONE grid point
   (j369 i430, 40.605 -111.622, ground 9,204 ft, its local peak): identical water and profile. Config model_cells
   RDPS grid_points = its own 3x3 grid points (rows 368-370, cols 429-431); cells._grid_weights blends each point
   bilinearly inside the grid square holding it (inverse bilinear from the square's corners; outside: nearest
   square, clipped). winds.earth_winds now locates block cells too (RDPS is the first grid-relative model with
   cells). Real check, RDPS 10/29 12z archived with the new code: the raw copy = the cloud's archived run exactly;
   blended (storm from 10/30 18Z) Alta 3.60" / Snowbird 3.63 / Solitude 2.87 / Brighton 3.14 / Mill-D 2.13 /
   Brighton SNTL 2.96 (nearest: 3.19 everywhere, Mill-D 1.53); ground 8,600-8,900 ft. Owner checked 06z + 12z:
   likes the water; the snow totals make sense given the models' freezing levels. Golden check IDENTICAL (old
   RDPS runs have no cells, so they stay nearest-point).
   RAIN/SNOW RULE STAYS 0 -> 1 C wet-bulb (owner 2026-10-29). Context: the owner says Alta 6" / Snowbird 0" from the
   same water "won't happen". Cause: Snowbird's point is 492 ft lower (~0.7 C warmer wet-bulb) and the 1 C ramp spans
   only ~700 ft. Tested on archived 10/29 runs: 0 -> 2 C gives RDPS Alta/Snowbird 8.8/4.0 (now 7.5/0.9), IFS 12.5/7.7
   (11.6/5.4); GFS/NAM (cold) unchanged. Revisit with the weekend storm's Alta vs Snowbird gauges (marginal storm).
5b. BUILT 2026-10-29 (in main, runs from the next release: pass step "Pattern maps", skipped by v1.4):
   src/cottonwoods/patterns.py + scripts/archive_patterns.py, config `patterns`, data/archive/patterns/model=*/init=*.
   GFS (0.25 deg source), GEFS mean + spread (0.5), IFS (00/12z) every 6 h to 240 h, all AREA-AVERAGED onto 1 deg,
   25-60N x 150-90W: z500, relative vort500 (GFS absv - f), u/v250, mslp, t850. PLUS (owner 2026-10-29, shortwaves
   matter) grid "native": 35-45N x 120-105W at each model's own grid points (GFS/IFS 0.25, GEFS 0.5), NOT averaged,
   to 120 h (patterns.inner). Real 48 h GFS: Utah-box peak vort500 3.3e-4 native vs 1.0e-4 at 1 deg. 3.4-4.3 MB/run,
   ~38 MB/day, ~1.2 GB/month in the bucket (season ~7 GB: watch R2's 10 GB free tier; float16 would halve it).
   Bucket pull skips patterns older than cloud.pull_pattern_days (3).
   Checked: GFS vs IFS same valid time r(z500) 0.997 at 48 h; vort 0.57 (GFS's 1 deg file is interpolated: 0.37).
   NOT YET: ECMWF ENS mean/spread (open data has members only), GDPS (global files; GeoMet WCS could cut the box).
   Original plan: coarse regional
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
