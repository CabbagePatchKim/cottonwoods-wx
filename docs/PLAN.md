# Agreed plan (owner, 2026-10-30)

Shown at the start of every Claude Code session by the SessionStart hook (scripts/collins_due.py). Update it as
steps finish; keep it short.

## Why: slow down and measure
The project has more machinery than evidence: one verified storm (dry), so the blends, regime factors, chances,
trends and guidance are all unproven. Until a few storms verify, the work is MEASURING, not building new features.

## Steps
1. [ ] Finish the pre-v1.7 dry run (DONE 10/30: all 10 steps ok; fixed guidance in block medians) and
       release v1.7 (owner approves the tag). RRFS / UU runs pre-seeded in the bucket (no first-pass catch-up).
       v1.7 = RRFS in (feed too), NAM / NAM Nest retired 11/3 12z, U of U guidance, HTML report, Verified step.
2. [ ] Let the weekend storm (Fri 10/30 - Mon 11/2) verify (~11/2-11/3). Enter Collins readings. Read the
       verification closely: which models, kinds (deterministic / guidance / ensemble) and methods did well;
       NAM Nest vs RRFS (the only side-by-side). Run scripts/regime_scores.py (laptop, after a bucket pull) by ~11/20.
3. [ ] FIRST BUILD AFTER THE PAUSE: NWS Salt Lake "Preliminary Storm Information" statements (PNSSLC) as automatic
       Collins storm totals (free; IEM archive back years: mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py?pil=PNSSLC).
       Lines like "Alta - Collins - 9662 ft  4 AM Thu  47.0  4.72" (snow, water) and a stated period ("from 8am Monday
       through 7am Thursday"). Checked: 2/19/2026 statement 47.0" vs the owner's 46" at the stake. Preliminary, only
       for notable storms: hand entries stay for the rest. Synoptic (live Collins) needs a $1,000 minimum contract:
       not an option (owner, 2026-10-30).
4. [ ] Then the backfill: score archived / re-pulled runs (NOAA HRRR / GFS archives, U of U since 2026-01, RRFS since
       2026-08) against those Collins totals and the journal, so models and methods are judged on many storms.
5. [ ] Only then decide from results: keep / fix / drop (regime factors, cell blend, the 0 -> 1 C rain/snow rule,
       PRISM idea), and the design changes (store raw + derive on read; a `variant` column instead of "GFS raw";
       one scores table; split CLAUDE.md into architecture / decision log / status).

## Not now
New features, new models, new report sections, unless a verified storm shows a real problem.
