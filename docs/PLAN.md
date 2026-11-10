# Agreed plan (owner, 2026-10-30)

Shown at the start of every Claude Code session by the SessionStart hook (scripts/collins_due.py). Update it as
steps finish; keep it short.

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
       (scripts/backfill_pns.py: 1,628 Collins rows, not yet pushed to the bucket). Journal check: 7 of 8 storms within
       1" of the stake (10/10, 49/50, 28/28, 10/10, 11.5/11, 46/47); 3/5/2026 stake 16" vs NWS 33": owner 11/10, NWS
       most likely counted two storms as one period (noon Wed - 6 PM Fri) where the owner counted one. So a statement's
       period can span two of our storms: the start check (pns.start_match_hours) catches it only when a start is
       stated. Backfill PUSHED to the bucket 11/10 (81 files, obs/pns). v1.11 NOT released (owner: not yet). Statements may give water only (11/01/2026: 2.43").
       Session reminder now warns only when the Worker is late or a storm has no total.
4. [ ] Then the backfill: score archived / re-pulled runs (NOAA HRRR / GFS archives, U of U since 2026-01, RRFS since
       2026-08) against those Collins totals and the journal, so models and methods are judged on many storms.
5. [ ] Only then decide from results: keep / fix / drop (regime factors, cell blend, the 0 -> 1 C rain/snow rule,
       PRISM idea), and the design changes (store raw + derive on read; a `variant` column instead of "GFS raw";
       one scores table; split CLAUDE.md into architecture / decision log / status).

## Small fixes to bundle with the step-3 release (noted 10/30)
- DONE in main 11/10 (owner request): each storm in the text Storms report ends with a trend table: per point UP /
  DOWN / SAME (median change of each model's storm snow vs its previous run, both full; SAME = under
  trends.steady_in 1"), plus models up / down / same (report.storm_trend_lines; same numbers as the HTML summary).
- DONE in main 11/10: Verified section legend line for "12h blocks" (runs x blocks x points, runs up to 72 h before
  THAT block, so mid-storm runs count; a count, not skill) and "block FA/miss" (read as a share of 12h blocks).
- 12 h block table: "." is explained nowhere and the code calls it "dry"; it means under 0.05" SNOW (dry OR rain:
  Fri 10/30 NAM / RDPS / IFS had 0.05-0.23" of rain at Alta, wet-bulb > +1 C). Add a legend line; also mark the
  first / last block when the storm covers only part of it ("Fri day" was 6 h of 12).
- Freezing levels: every model ran 230-530 ft above the SLC balloon on 10/30 12z (650 mb 1-2 C warmer at Alta).
  Scoring checked correct (balloon 3,258 m recomputed; our 700 mb heights within 10 m). Model error or Alta vs SLC
  35 km apart? Idea: store RAP's freezing level at each point (we keep only t_700) as an observed value over
  Alta's own column. Decide after the storm's wet balloons are in.

## Not now
New features, new models, new report sections, unless a verified storm shows a real problem.
