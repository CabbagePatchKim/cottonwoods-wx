"""LOCAL ONLY (owner, 2026-10-29): do the regime factors help? Every verified storm, plain Kuchera snow vs
regime-adjusted snow, scored by the cloud pass's own rules (bin score, runs, points). Never run by the cloud pass.

    python scripts/bucket_sync.py pull       # first: the live runs, storms, obs and Collins entries
    python scripts/regime_scores.py

Writes data/local/regime_bin_scores.csv (data/local is not synced to the bucket and not in git) and prints the
comparison. Limits: the laptop pulls only runs from the last cloud.pull_forecast_days (25), so rescore a storm within
~3 weeks of it verifying; and it uses today's regime_factors.csv, so refitting the factors changes past numbers.
"""
from datetime import timedelta

import pandas as pd

from cottonwoods import regimes, storms
from cottonwoods.archive.forecasts import load_forecasts
from cottonwoods.archive.obs import load_hand_obs, load_snotel, load_storm_totals
from cottonwoods.config import DATA_DIR, PROCESSED_DIR, load_config
from cottonwoods.report import regime_score_table

cfg = load_config()
factors = regimes.load_factors(PROCESSED_DIR / "regime_factors.csv")
if factors.empty:
    raise SystemExit("No data/processed/regime_factors.csv (pull the bucket, or run scripts/regime_factors.py).")
table = storms.load_storms()
verified = table[table["status"] == "verified"] if not table.empty else table
if verified.empty:
    raise SystemExit("No verified storms yet (pull the bucket first).")
first = pd.to_datetime(verified["final_start"]).min()
fc = load_forecasts(cfg, start=first - timedelta(hours=cfg["storms"]["max_lead_hours"]))
obs = load_snotel(start=first - timedelta(days=2))
both = storms.regime_bin_scores(table, fc, factors, storms.point_steps(obs, cfg), load_storm_totals(),
                                load_hand_obs(), cfg)
if both.empty:
    raise SystemExit("No verified storm has both its runs here and an observed snow total yet.")
out = DATA_DIR / "local" / "regime_bin_scores.csv"
out.parent.mkdir(parents=True, exist_ok=True)
both.to_csv(out, index=False)
n = both["storm_id"].nunique()
print(f"Plain vs regime-adjusted snow, {n} verified storm(s) with an observed snow total; every run within "
      f"{cfg['verify']['score_runs_hours']} h at every scored point (score: bin score; miss: average snow error), "
      f"then the average score by lead, plain / regime. Rows: {out}")
print(regime_score_table(both, cfg).to_string(index=False))
