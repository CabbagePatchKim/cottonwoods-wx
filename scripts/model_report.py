"""Print and save a side-by-side table of the newest run of every archived model, day by day, for each point.

Run automatically after each scheduled archive pass (scripts/run_models.bat); also fine by hand:
    python scripts/model_report.py
    python scripts/model_report.py --days 7 --points alta

Saved to data/reports/models_latest.txt (overwritten) and data/reports/models_<UTC time>.txt (kept).
Each cell: deterministic models "water_in snow_in" (Kuchera); ensembles "members_with_0.10in/N mean_water_in".
"-" means the run doesn't cover that day (e.g. HRRR beyond 48 h), or the day ends past the shown lead (config
report: deterministic 168 h, ensembles 240 h); "*" = the run started during the day (its part only).
The PRINTED report has no Collins values (the pass log is public); the saved one adds Alta Collins to the
"observed so far" row.
"""
import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd

from cottonwoods.archive.forecasts import load_forecasts
from cottonwoods.archive.obs import load_collins_readings, load_snotel
from cottonwoods.config import REPORTS_DIR, load_config
from cottonwoods import timefmt
from cottonwoods.report import day_windows, observed_today, render, staleness
from cottonwoods.storms import point_steps

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, help="default: config report.days")
ap.add_argument("--points", nargs="+", help="default: every configured point")
args = ap.parse_args()

cfg = load_config()
now = datetime.now(timezone.utc).replace(tzinfo=None)
# Staleness first: missing runs are listed at the top, so a model that's silently failing stands out.
warnings = staleness(cfg, now)
header = f"Model comparison, {timefmt.now_line(now)}. Times are Mountain (MDT/MST), 24-hour.\n"
header += ("\n".join(warnings) + "\n(check data\\logs\\models.log for [fail] lines)\n") if warnings else \
    "All models current: every run due in the last 2 days is archived.\n"
# Only runs from the last 2 days can be "newest", so don't read the whole archive.
fc = load_forecasts(cfg, start=now - timedelta(days=2))
points = args.points or list(cfg["points"])
if fc.empty:
    body = body_private = "No model runs archived in the last 2 days. Run scripts/archive_forecasts.py.\n"
else:
    # today's column so far (owner 2026-11-19): SNOTEL gauge water at every gauged point; Alta Collins new snow +
    # water only in the SAVED report (private): the printed one goes to the public pass log.
    day0 = day_windows(pd.Timestamp(now), 1, cfg["verify"]["window_end_utc_hour"])[0][0]
    psteps = point_steps(load_snotel(start=day0 - timedelta(days=1)), cfg)
    body = render(fc, cfg, points, args.days, partial=True, observed=observed_today(psteps, None, cfg, now, day0))
    body_private = render(fc, cfg, points, args.days, partial=True,
                          observed=observed_today(psteps, load_collins_readings(), cfg, now, day0))
print(header + "\n" + body)
text = header + "\n" + body_private
out = REPORTS_DIR
out.mkdir(parents=True, exist_ok=True)
(out / "models_latest.txt").write_text(text, encoding="utf-8")
(out / f"models_{now:%Y%m%d_%H%M}.txt").write_text(text, encoding="utf-8")
