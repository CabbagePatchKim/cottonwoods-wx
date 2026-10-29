"""Compare archived forecasts with observations.

    python scripts/verify.py                         # everything archived
    python scripts/verify.py --models HRRR --since 2026-11-01

Writes data/processed/verification_pairs.csv (one row per run x point x obs site x 24 h window) and
verification_summary.csv (per model, obs site and lead bin), and prints the summary.
"""
import argparse

import pandas as pd

from cottonwoods.archive.forecasts import load_forecasts
from cottonwoods.archive.obs import load_manual, load_obs
from cottonwoods.config import PROCESSED_DIR, ensure_dirs, load_config
from cottonwoods.models.registry import REGISTRY
from cottonwoods.verify import build_pairs, summarize

ap = argparse.ArgumentParser()
ap.add_argument("--models", nargs="+")
ap.add_argument("--since", help="only runs with init >= this date (UTC)")
args = ap.parse_args()

ensure_dirs()
cfg = load_config()
pd.set_option("display.width", 200)       # print wide tables without wrapping
pd.set_option("display.max_columns", 30)  # and without hiding middle columns behind "..."

# Every archived deterministic model run (filtered by the flags), stacked into one table. Ensembles are skipped
# by build_pairs anyway, and after a year they're 10+ million rows, so they aren't read at all.
deterministic = [m for m, spec in REGISTRY.items() if spec.members == 0]
want = [m for m in (args.models or deterministic) if m.upper() in deterministic]
# Blended models' unmodified runs are verified next to them, as "GFS raw" etc. (archive/forecasts_raw)
fc = pd.concat([load_forecasts(cfg, models=want, start=args.since),
                load_forecasts(cfg, models=want, start=args.since, raw=True)], ignore_index=True)
if fc.empty:
    raise SystemExit("No archived forecasts found. Run scripts/archive_forecasts.py first.")
# Only load observations for the dates the forecasts cover (plus 2 days of padding) to keep it small.
obs = load_obs(start=fc["valid_start"].min() - pd.Timedelta(days=2), end=fc["valid_end"].max() + pd.Timedelta(days=2))
if obs.empty:
    print("[warn] no archived observations; observed columns will be empty. Run scripts/archive_obs.py.")

# The core step: line up each forecast's 24 h window with what was observed in that window.
pairs = build_pairs(fc, obs, cfg, load_manual())
if pairs.empty:
    raise SystemExit("No complete verification windows yet (a run needs a full 24 h window ending at the configured hour).")
pairs.to_csv(PROCESSED_DIR / "verification_pairs.csv", index=False)

# Boil the pairs down to one row per model / obs site / lead-time bin: bias, error, hit rate.
summary = summarize(pairs, cfg)
summary.to_csv(PROCESSED_DIR / "verification_summary.csv", index=False)
print(summary.to_string(index=False) if not summary.empty else "No windows with observations yet.")
print(f"\n{len(pairs):,} windows paired; {pairs['swe_obs_in'].notna().sum():,} with observed liquid; "
      f"{pairs['new_snow_obs_in'].notna().sum():,} with hand-entered snowfall.")
print("Observed liquid = SWE change at SNOTEL sites, gauge precip at Synoptic sites (alta_collins).")
print("qpf_bias_ratio > 1 = model wetter than the obs site; pod = fraction of observed events the model had; "
      "far = fraction of model events that did not happen.")
