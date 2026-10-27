# Handoff: multi-season backfill plan and Alta stake data

Context: this note summarizes a design discussion held in a separate Claude chat (claude.ai). That chat built this
project scaffold; you are now running on the user's own machine with real internet access. Read CLAUDE.md first for
the project goals, then use this note for the backfill work. Nothing here has been run against live data. Everything
marked UNVERIFIED comes from web pages read during the chat, or from assumptions, and should be checked.

## Where the project stands
- Scaffold exists and passes offline tests on synthetic data (`python tests/run_offline.py`). Live-network code
  (SNOTEL client, Herbie HRRR/GFS pulls, ERA5 request, parquet writing) has never been run.
- Already built: forecast archiver (`scripts/archive_forecasts.py`, supports `--start/--end/--journal/--init`),
  SNOTEL archiver (`scripts/archive_obs.py`), verification (`scripts/verify.py`), journal entry tool
  (`src/cottonwoods/journal_entry.py`), Kuchera SLR per step, GFS APCP de-accumulation.
- The user has installed the package in editable mode in their Jupyter environment (Windows, Anaconda).
  SNOTEL triplets in `config/stations.yaml` are still null until `scripts/01_find_stations.py` is run.
- Do the first-run checklist in CLAUDE.md before any large backfill.

## Goal of this stage
Get several winters of model forecasts and observations into the archive so the raw-model bias, by site and by
flow regime, can be measured. That baseline decides whether a statistical correction layer (bias-corrected QPF,
better SLR than Kuchera, calibrated ensemble probabilities) is worth building. Keep a component only if it beats
the raw model on held-out seasons.

## What data exists, by source (checked via web pages during the chat)
| Source | Coverage | Notes |
|---|---|---|
| SNOTEL (NRCS AWDB) | many years | observation truth for SWE; client untested |
| HRRR | 2014-07-30 to present on AWS and Google Cloud (Herbie docs); Azure from 2021-03-21 | `sfc` and `prs` products; 00/06/12/18z reach f48, other hours f18 |
| GFS on AWS | page checked describes a rolling window of about 4 weeks, so NOT a multi-year source | UNVERIFIED how far back Herbie's GFS source reaches; test it for a 2022 date |
| GFS historical | NCAR GDEX dataset d084001: 0.25 degree, 2015-01-15 onward, 3-hourly to 240 h then 12-hourly | GRIB2; access terms (free or registration) NOT confirmed; page said NCAR stopped updating it in early 2026 with a maintained copy on AWS |
| Open-Meteo Previous Runs API | most models since Jan 2024; GFS 2 m temperature since Mar 2021 | fixed lead offsets `_previous_day1` to `_previous_day7`; includes GEM (Canadian), ECMWF IFS, ICON, NBM, HRRR, NAM; variables: precip, snowfall, 2 m temp, humidity, pressure, cloud, 10 m wind; NO pressure-level temperatures listed, so Kuchera cannot be computed from it; has non-commercial/commercial licence options, check terms |
| Pivotal Weather / COD maps | no archive | cannot be backfilled |

The Canadian models (GDPS/RDPS) and ECMWF open data have short public archives. Open-Meteo's GEM Global series
is the only route found to recover some Canadian history (confirm it corresponds to GDPS). Live archiving going
forward (cron) remains the only way to keep them long-term.

## Proposed backfill strategy (do not run all cycles for all days; it would take weeks)
1. Choose seasons: start with 3 winters (Nov-Apr); extend if results look promising.
2. Select days from SNOTEL first: days where Alta-Collins SWE rose by about 0.5 in or more, plus an equal number of
   randomly sampled dry days. The dry days are needed for false-alarm rates; without them every forecast looks good.
3. Pull a thin slice per day: HRRR 12z (or 00z) only, forecast hours 1-36, which covers a full 24 h verification window.
4. Time ONE run first. Report the per-run time and total estimate to the user before starting the backfill.
5. Run in monthly chunks using `archive_forecasts.py --start ... --end ...` (already skips archived runs, so it is restartable).
6. Delete the raw GRIB cache (Herbie `save_dir`, `data/raw/herbie`) after extraction; the parquet archive is small.
7. Order: HRRR + SNOTEL first, then Open-Meteo for lead-time precipitation skill, then older GFS.

## Missing pieces to build
- Storm-day selector (step 2), script in `scripts/`.
- Check what Herbie's GFS can reach for dates in 2021-2023; if only recent, add a puller for the NCAR GDEX archive
  (confirm access terms first).
- Open-Meteo Previous Runs puller (precip, snowfall, 2 m temp by lead day) for lead-time skill, including GEM Global.
- Importer for the Alta stake data (below).
- Later: ERA5 pressure-level pulls (`src/cottonwoods/models/era5.py`, needs a Copernicus CDS account) to label flow
  regimes and fit a better SLR over many winters.

## Alta Collins stake data (user-reported, not yet seen)
- User says Alta's website has snowfall at the Collins stake going back about 20 years, "under season totals" at
  https://www.alta.com/weather.
- A plain page fetch only returned section headings: **Season Totals**, **Monthly Averages 1980 - Present**,
  **Monthly Snow Totals**, with no tables or download links (content is probably loaded by JavaScript).
- UNKNOWN: whether the data is daily, monthly, or seasonal; which columns exist (new snow, storm total, base depth,
  water content); whether anything is downloadable; terms of use.
- Granularity decides usefulness:
  - Daily (24 h new snow, ideally with water content): use for observed SLR over 20 years (stake snowfall divided by SNOTEL
    SWE change or Alta's own water content), per-storm verification, regime climatology, and base rates.
  - Monthly or seasonal only: still useful for climatology and base rates by month, but not for storm-level SLR or verification.
- Next step is to inspect, not to scrape. Suggested approach: ask the user what the Season Totals section shows;
  they can open Edge DevTools (F12) > Network > Fetch/XHR, reload, and look for a JSON response holding the numbers.
  Read Alta's site terms before any automated fetching, keep requests slow and few, and consider asking Alta for the data.
- Before pooling 20 years: check the daily reading time (verification windows currently end at 12 UTC = 5 am MST,
  set in `config/stations.yaml` under `verify`), the stake's elevation (config `points.alta` uses 8700 ft), and plot
  yearly totals to catch jumps from changes in measurement practice.
- Target format: `data/manual/new_snow.csv` columns `date,site,new_snow_in,swe_in,notes` with `site=alta`, which
  `verify.py` already reads.

## Suggested first message to the user's Claude Code session
Read CLAUDE.md and HANDOFF_backfill.md. Do the first-run checklist first (SNOTEL triplets, SNOTEL pull, one HRRR
pull for 2023-12-08; journal years for the four early storms were corrected to 2023-24). Then build the storm-day selector and time one HRRR run, and tell me the total estimate
before starting any backfill. Do not scrape alta.com until I have shown you what the Season Totals data looks like.

## Open questions for the user
- What does Alta's Season Totals section show (daily, monthly, seasonal), and what columns?
- At what hour is the Collins stake read, and at what elevation is it?
- Which site were the journal "actual" totals measured at (still blank in `data/journal/storms.csv`)?
- Actuals missing for the 2/16-2/18/2026 storms and 3/5/2026 (use `run_interactive()` in `journal_entry.py`).
