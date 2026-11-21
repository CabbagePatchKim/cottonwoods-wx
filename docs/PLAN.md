# Agreed plan (owner, 2026-10-30)

Shown at the start of every Claude Code session by the SessionStart hook (scripts/collins_due.py). Update it as
steps finish; keep it short.

## LEARNING MODEL PLAN (owner 2026-11-19): docs/LEARNING_PLAN.md (step 6 design; v0 + switches; GFS dryness check first)

## FIRST THING NEXT SESSION (2026-11-20): ask before anything else
1. Did the sequential backfill finish? (one job at a time after the 11/19 low-memory stop of all four jobs): IFS
   2024-25 (80/86 at the stop), GFS + HRRR 2024-25, 2023-24, 2022-23, then 500 mb maps (0 h analyses 4 winters, then
   GFS forecast maps). QUEUED AFTER IT (11/19 night): IFS late 2023-24 (first_init 2024-02-01) + 3 IFS runs re-fetched
   past 144 h, then ENS (2024-02 on, 187 runs, ~72 s each), then GEFS (4 winters, 295 runs, ~175 s each): done ~Sat
   11/21. Check data/local/backfill/runs_log.csv + maps folders; rerun the same commands (they skip what is on disk).
   RUN ONE AT A TIME (7.8 GB laptop; close other programs).
2. Then: backfill_scores.py per winter; timeheight_scores.py --winters 2025-26 2024-25 2023-24 2022-23; the GFS
   dryness check (LEARNING_PLAN section 0).
3. RELEASED v1.13 (owner approved 11/19 "publish 1.13"; tag on 21b39ed, CI green; golden: only expected
   differences; dry run on a bucket copy ok). CHECK the first v1.13 pass (Worker ~05:50Z 11/20): log "Running
   approved release v1.13"; feed meta code_version v1.13 with park_city in points (its numbers fill in as new runs
   arrive); cwx-feed README = the v1.13 manual; phone viewer Storms "Observed at Alta" + Collins block rows, Models
   "Thu-Fri" headers / "*" / "obs so far"; the PUBLIC pass log has NO Collins values. Friend heads-up: new feed key
   park_city (manual v1.13); GDPS at Brighton shifts slightly.
   RELEASED v1.13.1 (owner 11/20 "push 1.13.1"; dd7e080): NCAR GDEX answers 403 for every GFS file since 11/19 18z;
   live GFS now falls back to AWS on any GDEX failure. CHECK its first pass: "Running approved release v1.13.1",
   "[gfs] GDEX failed ... trying AWS", GFS runs archived and the meta.json stale-GFS warning gone.
   Also 11/20: docs/FORECAST_CHECKLIST.md (+ .txt), docs/STORM_NOTES.md (11/19-20 storm; owner's 11/23 forecast to
   score Monday). Next: LEARNING_PLAN steps 0 (GFS dryness check - Atwater ran far under Collins on 11/19-20) and 2.

## IN MAIN 11/19 (not released)
- U of U parser: "0:1 /  -%" (zero snow ratio, UUGFS) dropped hours; fixed + test (1 of 278 live runs affected).
- park_city point at the Thaynes Canyon SNOTEL (IN THE PUBLIC FEED from v1.13, owner 11/19; label "Thaynes Canyon SNOTEL"), verified on its gauge and
  NWS "Park City Mountain" totals (pns.sites); thaynes_canyon in config snotel (obs fetched every pass; history
  2016-10+ backfilled on the laptop, not pushed). GDPS cells 2x3 (a column at -111.45). cells.blend_points now
  checks each point's own cells (old runs keep blending their old points).
- Reports (owner 11/19): Storms page "Observed at Alta" (Collins so far / NWS / scored-as, recent storms) and Collins
  rows in the 12 h blocks of a storm under way; Models: "Thu-Fri 11/20" headers, partial day for runs started
  mid-day ("*"), "obs so far" row. Collins only in SAVED files (pass log is public: printed versions are SNOTEL-only);
  the feed unchanged. Chances table: "most likely bins".
- golden.py prints the first changed lines of a differing report.
- Laptop tools (not in the pass): time-height (BUFKIT), shortwaves / PVA, backfill_patterns / backfill_timeheight /
  timeheight_scores, backfill models HRRR + UUHRRR + UUGFS (first_init 2026-01-01), backfill.runs_by_lead.
- 2025-26 backfill complete: GFS, IFS, HRRR (23), UUGFS (36), UUHRRR (12); RRFS impossible (NOAA archive from 2026-08).
  Water ratio vs SNOTEL at 24 h: HRRR 0.73, UUHRRR 0.85, UUGFS 0.66, GFS 0.45, IFS 0.33.
  Time-height first look (1 winter): observed heavy blocks rise with moist depth (<1 km 0.16" / 6 h, >3 km ~0.5");
  models driest when their own forecast is shallow / growth zone unsaturated; Thaynes / Brighton 0.58 -> 0.94 as the
  cross-barrier wind rises (pillows agree: not undercatch); onset: no signal yet.

## DONE 2026-11-18 (evening): GFS 2025-26 backfill complete, 69 of 69 runs (last 18 archived, exit 0)
GOLDEN CHECK 11/18 for v1.12: every run IDENTICAL; both reports IDENTICAL on the snapshot's own runs (the first
"DIFF report storms/models" was runs archived after the 11/11 snapshot). golden.py check now uses only the
snapshot's runs for the reports. v1.12 is ready to release on the owner's approve (CI green first).
SCORED 11/18: scripts/backfill_scores.py --winter 2025-26 (new, laptop) -> data/local/backfill/scores_2025-26.csv.
23 storms, 12 with snow totals. BOTH far too dry at Alta: water ratio GFS 0.45/0.42/0.29 at 24/48/72 h, IFS
0.33/0.30/0.26 (live 2026-27, 2 storms: ~0.5-0.6). GFS beats IFS: snow MAE 7.2" vs 10.7", bin avg -1.4 vs -3.5,
right bins 7/36 vs 1/36, closer on water 39 of 69. Blend beats raw for both (GFS 7/36 vs 3/36). 168 of 276 runs stop
1-2 h before the storm end (backfill fetched to the exact last hour, 3-hourly steps): scored over the covered window,
obs water re-totalled to match (cut_h). Fix backfill.plan_runs to round up to a step before widening.
Suspect obs: 2025121721 (1.1" in 3 h), 2025110900 (both models 0.00). Next: regime factors on vs off (stage 4). Storm 2026111718 (11/17-18) not verified as of 11/18 1713 MST: a lone 0.1"
SNOTEL tick at 11/18 18z reset the 12 h dry clock; check it verifies (and doesn't merge with 2026111906b).

## FIRST THING NEXT SESSION (2026-11-17): ask before anything else
1. RELEASED v1.12 (owner approved 11/18, tag on fcd9e85, CI green). CHECK the first v1.12 pass in the phone viewer /
   bucket log: code v1.12, ENS 06/18z archived again. THEN re-pull the ENS 06/18z runs 11/12 18z-11/17 the failure log
   skipped (Google mirror). Was: (a) partial Collins coverage (owner 11/17: a Worker total missing more than
   storms.collins_max_missing_hours (0) of the window's hours yields to the NWS statement, then the hand total; the
   partial total is the last resort, labelled "N of M h missing"; no verified storm changed); (b) ENS 06/18z FIX:
   v1.11 mapped ENS off-hour runs to "waef" (= ECMWF's WAVE ensemble, no tp), so EVERY live ENS 06/18z run failed from
   11/12 18z on (IFS fine). Now "enfo" again (real pull 11/17 06z: 50 members). After release: re-pull the missed ENS
   06/18z runs 11/12-11/17 (failure log skips them after 4 tries; Google mirror still has them).
   BEFORE RELEASING: rerun tools/golden.py check (11/17 run was stopped for low memory; close other programs).
2. GFS 2025-26 backfill: DONE 11/18, 69 of 69 runs.
   Resume (skips runs already on disk): python scripts/backfill_runs.py --winter 2025-26 --model GFS
   ~100-120 s per run (~1.5 h left). Keep the laptop awake: Modern Standby 11/17 21:31-22:14 stalled one run 46 min.
   10/01 + 10/02 00z re-pulled from AWS (complete); their GDEX copies were set aside in the session scratchpad.
   Then: score GFS vs IFS for 2025-26 (stage 4), time 1 HRRR run (ask first), its pilot.
DONE 11/17: corrected NWS statements pushed to the bucket (80 months 2016-10..2026-10; obs/pns). v1.11 first pass log
checked (11/12 14:59Z: all steps ok, NWS statements step ok). Obs step FAILED 4 passes 11/16 21Z-11/17 06Z: SNOTEL
AWDB 502s; catch-up refilled every hour (checked). UUHRRR 11/16 18z-11/17 12z never posted by U of U (404).
Storm 11/14 verified: Collins 5" / 0.49" (18/18 h), SNOTEL 0.70"; most models too snowy (SLR), water close.

## Collins Worker (done 2026-11-03)
Locked (X-CWX-Key; 403 without it, checked), deployed, GitHub secrets CWX_COLLINS_URL / CWX_COLLINS_KEY set,
pushed to main, released as v1.8 (owner approved 11/03). The key's only copy is ~/.cwx_collins_key (laptop, never
in git). v1.9 released 11/03 (below-ground levels, retired models). Collins storm window "option B" (owner 11/03):
released as v1.10 (11/03). Next: step 3 (PNSSLC).

## Why: slow down and measure
The project has more machinery than evidence: one verified storm (dry), so the blends, regime factors, chances,
trends and guidance are all unproven. Until a few storms verify, the work is MEASURING, not building new features.

## Steps
1. [x] Finish the pre-v1.7 dry run (DONE 10/30: all 10 steps ok; fixed guidance in block medians) and
       release v1.7 (RELEASED 10/30 ~08:13Z, tag on 3c43be7; check its first pass log). RRFS / UU runs pre-seeded in the bucket (no first-pass catch-up).
       v1.7 = RRFS in (feed too), NAM / NAM Nest retired 11/3 12z, U of U guidance, HTML report, Verified step.
2. [x] DONE 11/10. Weekend storm verified (Sun 11/01 0100 - Mon 11/02 1400 MST) from the Worker: 16" / 2.35"
       (no hand entry needed). Read with the owner: best over 72 h UUGFS, GDPS, GFS; worst RDPS (9.8" too wet; blend
       made it worse) and IFS; most last runs short (HRRR / RRFS / IFS 10-12"). NAM Nest last run right bin but 3.73"
       water (60% wet); RRFS smaller snow error over all runs (3.1 vs 4.3") but more block FA/miss. regime_scores.py
       11/10 (3 storms): regime-adjusted = plain within 0.1 for every model (no effect yet).
       FOUND: the storm's FORECAST REGIME label ("N mild 100%") came from IFS 11/02 12z, a run made in the storm's last
       hours (the defining run keeps updating while a storm is live). Runs before the storm said S/E warm -> SW -> NW
       (close to observed S/E -> SW -> N). FIXED 11/10 in main (owner approved): storms.csv regime_model / regime_init =
       the defining run until the storm starts, then FROZEN (fc_flow / fc_modes / fc_subperiods with it; the window
       still updates); storms verified before the fix are re-labelled once from the newest runs issued before their
       start (regime_before_start), and their block scores re-scored.
3. [x] BUILT 11/10 in main, NOT RELEASED: NWS Salt Lake storm statements (PNSSLC) as Collins storm totals
       (obs/pns.py; both layouts: the "Preliminary Storm Information" table and the 2023-24 "SNOWFALL / PRECIPITATION
       REPORTS" sections). Owner 11/10: NO MORE HAND ENTRIES; order = Worker hours, then NWS statement, then an old
       hand total (storms.observed_snow). Pass obs step fetches the last pns.days; laptop backfill 2016-10 on
       (scripts/backfill_obs.py pns: 1,628 Collins rows, not yet pushed to the bucket). Journal check: 7 of 8 storms within
       1" of the stake (10/10, 49/50, 28/28, 10/10, 11.5/11, 46/47); 3/5/2026 stake 16" vs NWS 33": owner 11/10, NWS
       most likely counted two storms as one period (noon Wed - 6 PM Fri) where the owner counted one. So a statement's
       period can span two of our storms: the start check (pns.start_match_hours) catches it only when a start is
       stated. Backfill PUSHED to the bucket 11/10 (81 files, obs/pns). v1.11 released 11/12. Statements may give water only (11/01/2026: 2.43").
       Session reminder now warns only when the Worker is late or a storm has no total.
4. [ ] Then the backfill: score archived / re-pulled runs (NOAA HRRR / GFS archives, U of U since 2026-01, RRFS since
       2026-08) against those Collins totals and the journal, so models and methods are judged on many storms.
       STARTED 11/10 (owner). Stages: (1) storm list, (2) pilot re-pull 2025-26 GFS / HRRR / ECMWF at 24/48/72 h,
       (3) widen, (4) season tables + regime factors on vs off -> step 5.
       (1) DONE 11/10: SNOTEL hourly backfilled 2016-10 to 2026-05 (scripts/backfill_obs.py snotel, laptop only, not
       pushed); backfill.py + scripts/backfill_storms.py -> data/local/backfill/storms.csv: 696 storms, 269 with
       >= 0.5" water, 165 of those with an NWS snow total; journal storms all match NWS within 1" but 3/5/2026.
       Alta = Atwater from 2022-23, Snowbird SNOTEL before (config backfill.fallback_sites).
       FOUND + FIXED (live rule too, owner to hear): older SNOTEL hourly data has one-hour spikes (Atwater / Snowbird
       34.5 -> 63.5 -> 34.5" daily at 08Z) and sensor jumps (Atwater 4/17/2023 +14" in 2 h, pillow flat); the gauge
       rule counted them (winters of 400-1,500"). gauge.py rules 4-5 (config gauge.glitch_hours 3, max_hourly_in
       1.0): 151 of 314 months changed, winters now 35-58"; live Oct-Nov 2026 months unchanged to 0.01".
       (2) pilot 11/10: backfill uses GFS from AWS first + 4 NOAA downloads (config backfill). ECMWF 2025-26: 71 runs,
       ~1 min each, 7 MB total; every 06/18z run failed at first: the mirrors file older off-hour runs as "scda"
       (ENS "waef"), not "oper". FIXED (herbie_generic.herbie_args; recent runs resolve to the same file, live
       unchanged). 11/10 end: 40 of 69 archived (all 00/12z); the 29 06/18z runs were NOT retried until 11/11:
       retry DONE, all 69 IFS runs archived (manifest 69, none failed).
       GFS 11/11: the two slow timings (1,211 s / 532 s) were GDEX (before AWS-first). AWS: 10-03 00z 56 h in 63 s,
       10-02 00z 80 h in 87 s (~1.1 s per forecast hour; 2025-26 plan 69 runs / 5,121 h ~ 1.5 h). AWS vs GDEX same
       run: every shared row identical; GDEX lacked 2 steps per run (10-01 320/340, 10-02 240/260), AWS complete.
       Next: GFS pilot (~1.5 h, ask), time 1 HRRR run (ask the owner first: memory), then its pilot.
5. [ ] Only then decide from results: keep / fix / drop (regime factors, cell blend, the 0 -> 1 C rain/snow rule,
       PRISM idea), and the design changes (store raw + derive on read; a `variant` column instead of "GFS raw";
       one scores table; split CLAUDE.md into architecture / decision log / status).
6. [ ] The learning layer ("CWX"), agreed 2026-11-19: docs/LEARNING_PLAN.md. Starts with the GFS dryness check
       (SNOTEL vs Collins / NWS, blended vs nearest point), then v0 (water correction, timing, blend weights, SLR model,
       calibrated bins; season-out tested, partial pooling, capped) and the article-derived switches tested one at a
       time; v0 + v1 run live and frozen through 2026-27 before any feed decision.

## Review of the 11/10 work (11/11, in main, not released)
Dry run of main's storms step on a bucket copy: ok (frozen regime: weekend storm now S/E -> SW -> NW from GDPS
11/01 00z; trend table + NWS line render). FIXED: (1) cells.blend_points blended the int `filled_values` of
hole-filled runs -> pandas 3 TypeError (golden check crashed; the cloud re-derive would at the next DERIVE_VERSION);
bookkeeping now not blended. (2) NWS statement parser: "updated as of / as of" times read as starts; NWS date typos
(Sep for Nov, Feb for Mar) -> weekday decides past pns.max_storm_days (10); weekday start after the statement -> none;
"thunderstorms" read as Thursday (whole names only); a row time later on the statement's day (1/11/2025 slip) ->
statement time, not a week back. 9 stored starts + 1 reading corrected; no backfill storm's NWS total changed.
(3) gauge rule 5 per elapsed hour (no stored gap rose > 0.4": no effect). Golden IDENTICAL, 197 tests, ruff clean.
RESOLVED 11/17 (owner): a partial Collins total yields to the NWS statement (storms.collins_max_missing_hours).

## Small fixes to bundle with the step-3 release (noted 10/30)
- DONE in main 11/10 (owner request): each storm in the text Storms report ends with a trend table: per point UP /
  DOWN / SAME (median change of each model's storm snow vs its previous run, both full; SAME = under
  trends.steady_in 1"), plus models up / down / same (report.storm_trend_lines; same numbers as the HTML summary).
- DONE in main 11/10: Verified section legend line for "12h blocks" (runs x blocks x points, runs up to 72 h before
  THAT block, so mid-storm runs count; a count, not skill) and "block FA/miss" (read as a share of 12h blocks).
- DONE in main 11/10: 12 h block table legend ('"." = under 0.05" of snow (dry or rain)') and a block the storm fills
  only part of is headed with its hours ("Sun ngt 6h").
- DONE in main 11/10: FEED snow_level_ft gains start_below_ground / end_below_ground (owner chose ground value + flag);
  manual v1.11 entry written (also covers the frozen flow / modes / parts notes, which the feed's notes show).
- DONE in main 11/10: .gitattributes keeps LF in the working copy (bat files CRLF); renormalize changed nothing.
- CHECKED 11/10, NOT A BUG: the season freezing table's "RDPS raw +5329 / GFS raw +2563" outliers. (a) SLC balloon
  11/03 12z crossed 0 C at ~7,800 ft and stayed -0.7 to -2.1 C up to ~13,600 ft: in a near-isothermal layer a model
  1-2 C warm puts its level 3,000-5,500 ft higher (every model, even GFS at 6 h). (b) 8-14 day runs with the level
  below their ground (~6,700-7,000 ft) vs balloons ~13,000 ft: real misses. Idea (not built): show a median miss
  next to the mean so a few such balloons don't swing the 3-7 d averages.
- Freezing levels: every model ran 230-530 ft above the SLC balloon on 10/30 12z (650 mb 1-2 C warmer at Alta).
  Scoring checked correct (balloon 3,258 m recomputed; our 700 mb heights within 10 m). Model error or Alta vs SLC
  35 km apart? Idea: store RAP's freezing level at each point (we keep only t_700) as an observed value over
  Alta's own column. Decide after the storm's wet balloons are in.

## Not now
New features, new models, new report sections, unless a verified storm shows a real problem.
