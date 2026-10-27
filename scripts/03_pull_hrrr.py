"""Pull HRRR point data for one journal forecast and compute Kuchera snowfall per step.

Example:
    python scripts/03_pull_hrrr.py --storm 2023-12-08
Uses the HRRR init listed in journal_forecasts.csv for that storm. Falls back to --init if the journal has none.
Only 00/06/12/18z runs are allowed (config archive.cycles_utc); those are the HRRR runs that reach 48 h.
"""
import argparse

import pandas as pd

from cottonwoods.archive.forecasts import derive_run_fields
from cottonwoods.config import PROCESSED_DIR, RAW_DIR, ensure_dirs, load_config
from cottonwoods.journal import load_forecasts
from cottonwoods.models.hrrr import pull_run
from cottonwoods.models.registry import check_cycle

# argparse turns command-line flags into attributes: `--max-fxx 18` becomes args.max_fxx == 18
# (dashes become underscores). "fxx" = forecast hour, i.e. hours after the model's start (init) time.
ap = argparse.ArgumentParser()
ap.add_argument("--storm", required=True)
ap.add_argument("--init", help="override init time, e.g. 2024-12-07T00:00")
ap.add_argument("--max-fxx", type=int, default=48)
args = ap.parse_args()

ensure_dirs()
cfg = load_config()

# Pick the model init (start) time: from --init if given, otherwise from the HRRR line in the journal.
if args.init:
    init = pd.Timestamp(args.init).to_pydatetime()
else:
    fc = load_forecasts()
    row = fc[(fc.storm_id == args.storm) & (fc.model == "HRRR")]
    if row.empty:
        raise SystemExit(f"No HRRR entry for {args.storm} in the journal; pass --init.")
    # .iloc[0] = first matching row. tz_localize(None) drops the "UTC" label; times stay in UTC but plain
    # (time zone-free) timestamps are what the rest of the code expects.
    init = row.iloc[0].init_utc.tz_localize(None).to_pydatetime()

# Download forecast hours 1..max_fxx and pull values at each point. GRIB files are cached in data/raw/herbie,
# so a re-run for the same storm is much faster.
check_cycle(init, cfg)  # stops here for e.g. an 03z run, which only reaches 18 h
df = pull_run(init, range(1, args.max_fxx + 1), cfg["points"], save_dir=str(RAW_DIR / "herbie"))
if df.empty:
    raise SystemExit("Nothing pulled. Check network access and Herbie/cfgrib/eccodes installs.")

# The same derivation every archived run gets (one place: archive.forecasts.derive_run_fields): flow regime,
# precip mode, station profile at each resort's real elevation, wet-bulb snow/mix/rain, then Kuchera per hour.
# Totals are summed afterward. Applying one ratio to the storm-total precip would be wrong (see CLAUDE.md).
df = derive_run_fields(df, cfg)

out = PROCESSED_DIR / f"hrrr_{args.storm}_{init:%Y%m%d%H}.parquet"
df.to_parquet(out)
print(df.groupby("point")["snow_in"].sum().round(1).rename("model snowfall (in), station profile + Kuchera, summed over pulled hours"))
print(f"wrote {out}")
