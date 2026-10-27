# cottonwoods-wx

Data pulls and verification tooling for Cottonwood Canyons snowfall forecasting.
Open this folder in Claude Code; `CLAUDE.md` has the project context.

## Setup
```bash
mamba env create -f environment.yml     # conda-forge gives you eccodes, which cfgrib needs
conda activate cottonwoods-wx
pytest                                   # offline tests
```
ERA5 needs a free Copernicus CDS account and `~/.cdsapirc`.

## Run order
```bash
python scripts/04_score_journal.py       # works offline: your journal vs actuals
python scripts/01_find_stations.py       # find SNOTEL triplets -> paste into config/stations.yaml
python scripts/02_pull_snotel.py         # observed SWE for every storm window
python scripts/03_pull_hrrr.py --storm 2023-12-08   # HRRR points + Kuchera snowfall
```

## Automatic archiving and verification

Layout (all git-ignored; back up `data/archive/` yourself):
```
data/archive/forecasts/model=HRRR/init=2026110212.parquet   one file per model run, never edited
data/archive/forecasts/model=GFS/init=2026110212.parquet
data/archive/obs/snotel/ym=2026-11.parquet                  hourly SNOTEL, UTC, revisions replace old values
data/archive/manifest.csv                                   what was archived, completeness, when
data/manual/new_snow.csv                                    your hand-entered Alta/Snowbird/Brighton snowfall
```

One-time setup, then schedule the three commands below:
```bash
# 1. fill in SNOTEL triplets (config/stations.yaml) using scripts/01_find_stations.py
# 2. backfill the storms in your journal (HRRR/GFS archives and SNOTEL go back years)
python scripts/archive_obs.py --journal
python scripts/archive_forecasts.py --journal
python scripts/verify.py
```

Schedule (cron, `crontab -e`; adjust the paths to your install and environment):
```cron
# model runs: every 3 h at :40, tries the latest 2 cycles of each model, skips ones already archived
40 */3 * * *  cd ~/cottonwoods-wx && ~/miniforge3/envs/cottonwoods-wx/bin/python scripts/archive_forecasts.py >> data/archive/cron.log 2>&1
# observations: daily, re-fetches the trailing 7 days
20 8 * * *    cd ~/cottonwoods-wx && ~/miniforge3/envs/cottonwoods-wx/bin/python scripts/archive_obs.py >> data/archive/cron.log 2>&1
# verification: daily after the obs pull
40 8 * * *    cd ~/cottonwoods-wx && ~/miniforge3/envs/cottonwoods-wx/bin/python scripts/verify.py >> data/archive/cron.log 2>&1
```
macOS: cron works, or use launchd. Windows: Task Scheduler running the same commands. If the machine is asleep when a job is due,
nothing is lost for HRRR/GFS (each run tries the latest 2 cycles and `--start/--end` backfills gaps), but the latest 2 cycles are the
only automatic catch-up, so a multi-day gap needs one manual `archive_forecasts.py --start ...` run.

`verify.py` pairs each archived run with observed windows (24 h ending 12 UTC = 5 am MST) and reports, per model and lead time:
`qpf_bias_ratio` (model liquid / SNOTEL SWE change), `qpf_mae_in`, `pod`/`far` for measurable events, and `snow_bias_ratio`
(model Kuchera snowfall / your hand-entered new snow). To add hand-entered snowfall, append rows to `data/manual/new_snow.csv`.

Adding a model: add a `ModelSpec` and puller in `src/cottonwoods/models/registry.py`. GDPS/RDPS and ECMWF open data are the
most valuable additions because public archives of them are short: archiving them live is the only way to build a record.

## Notebook
```bash
jupyter lab notebooks/01_explore.ipynb
```
Sections 1-3 (journal scoring, Kuchera vs 10:1, regime labels) run offline. Sections 4-5 (SNOTEL, HRRR) need network and the setup above.

## Status
Tested offline (24 tests, synthetic data): Kuchera, layer Tmax, regime labels, grid-point lookup, journal loading,
precip de-accumulation, cycle scheduling, archive idempotence, SNOTEL UTC conversion and revision replacement, verification pairing.
**Not yet run against live data:** SNOTEL client, Herbie HRRR and GFS pulls, ERA5 request, and parquet writing (no pyarrow where this was built). Expect small fixes on first run
(variable names, API response shape). Check station coordinates and SNOTEL triplets in `config/stations.yaml`.

## Data you should review
`data/journal/storms.csv`: window dates are guesses (`window_confirmed=false`), the 4/14 storm is assumed to be 2025,
and actuals are blank for 2/16-18 and 3/5. `actual_site` is blank everywhere; fill it in.
