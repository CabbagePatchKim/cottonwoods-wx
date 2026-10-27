"""Track storms and verify them once they're over. Run after each archive pass (scripts/run_models.bat).

    python scripts/storms.py

1. Finds storm events in the newest run of each model (12 h dry gap ends a storm); the longest model window
   becomes the storm's timeframe for every model. Stored in data/archive/storms.csv.
2. For storms SNOTEL shows are over (12 h with no measurable precip), scores every archived run that covered
   the storm (water, Kuchera snow, ensemble members) against observed water: data/archive/storm_scores.csv.
3. Prints each upcoming storm with every model's newest total over the storm window, and saves it to
   data/reports/storms_latest.txt.
"""
from datetime import datetime, timedelta, timezone

import pandas as pd

from cottonwoods import storms, timefmt
from cottonwoods.report import render_storms, render_verified
from cottonwoods.archive.forecasts import load_forecasts
from cottonwoods.archive.obs import load_hand_obs, load_snotel, load_soundings, load_storm_totals
from cottonwoods.obs.sounding import summarize
from cottonwoods.config import REPORTS_DIR, load_config

cfg = load_config()
now = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
# Runs from the last 20 days cover any storm still being tracked; obs from the last 25 days cover its window.
fc = load_forecasts(cfg, start=now - timedelta(days=20))
obs = load_snotel(start=now - timedelta(days=25))
if fc.empty:
    raise SystemExit("No archived model runs in the last 20 days.")

snd = load_soundings(start=now - timedelta(days=25))
table = storms.run(fc, obs, cfg, now, soundings=summarize(snd) if not snd.empty else None)
text = f"Storms, {timefmt.now_line(now)}. Times are Mountain (MDT/MST), 24-hour.\n\n" + render_storms(table, fc, cfg)
verified = render_verified(table, storms.load_scores(), load_storm_totals(), cfg, now, readings=load_hand_obs())
if verified:
    text += "\n" + verified
newly = table[(table.status == "verified") & (table.updated_at == now)]
for _, st in newly.iterrows():
    text += (f"\nVERIFIED storm {st.storm_id}: {timefmt.clock(st.final_start)} - {timefmt.clock(st.final_end)}, "
             f"observed {st.obs_water_in:.2f}\" water (SNOTEL), observed flow {st.obs_flow}. "
             f"Scores in data/archive/storm_scores.csv\n")
print(text)
out = REPORTS_DIR
out.mkdir(parents=True, exist_ok=True)
(out / "storms_latest.txt").write_text(text, encoding="utf-8")
