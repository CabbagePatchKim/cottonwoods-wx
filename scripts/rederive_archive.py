"""Re-derive archived runs made by an older derivation (archive/forecasts.DERIVE_VERSION), in place.

Derived fields (snow, ratio, regimes, the cell blend) are computed when a run is archived, so a fix to the derivation
only reaches new runs. This strips them from every older run and derives them again with the current code and
config; the pulled numbers are kept, apart from the one-time wind turn (winds.py). Version 2 (2026-10-29):
earth-relative winds for HRRR / NAM Nest / NAM / RDPS, the 2x2 cell blend, the surveyed Solitude/Brighton elevations.
Safe to run any time: runs already at the current version are skipped. Each scheduled pass runs it after archiving.
    python scripts/rederive_archive.py                 # every run in data/archive/forecasts
    python scripts/rederive_archive.py --models HRRR RDPS --dry-run
"""
import argparse
import time

from cottonwoods.archive.forecasts import DERIVE_VERSION, DERIVED, derive_run_fields, forecast_dir
from cottonwoods.archive.io import read_df, write_df
from cottonwoods.config import load_config

ap = argparse.ArgumentParser()
ap.add_argument("--models", nargs="+", help="default: every model in the archive")
ap.add_argument("--dry-run", action="store_true", help="count the runs that would be re-derived, change nothing")
args = ap.parse_args()

cfg = load_config()
t0, done, current = time.time(), 0, 0
for path in sorted(forecast_dir().glob("model=*/init=*.*")):
    model = path.parent.name.split("=")[1]
    if args.models and model not in {m.upper() for m in args.models}:
        continue
    df = read_df(path)
    if "derive_version" in df.columns and (df["derive_version"] >= DERIVE_VERSION).all():
        current += 1
        continue
    done += 1
    if not args.dry_run:
        write_df(derive_run_fields(df.drop(columns=[c for c in DERIVED if c in df]), cfg), path)
print(f"[rederive] {'would re-derive' if args.dry_run else 're-derived'} {done} runs to version {DERIVE_VERSION}, "
      f"{current} already current ({time.time() - t0:.0f}s)")
