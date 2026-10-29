"""Track storms and verify them once they're over. Run after each archive pass (scripts/run_models.bat).

    python scripts/storms.py

1. Finds storm events in the newest run of each model (12 h dry gap ends a storm); the longest model window
   becomes the storm's timeframe for every model. Stored in data/archive/storms.csv.
2. For storms SNOTEL shows are over (12 h with no measurable precip), scores every archived run that covered
   the storm (water, Kuchera snow, ensemble members) against observed water: data/archive/storm_scores.csv.
   Once a verified storm has an observed snow total, every deterministic run gets the owner's bin score:
   data/processed/bin_scores.csv (rebuilt each pass; hand totals often arrive later).
3. Gauge vs snow pillow at every SNOTEL site, per 24 h window with the balloon flow (the undercatch evidence):
   data/processed/gauge_pillow_days.csv (recent days recomputed each pass; the whole archive the first time) and
   undercatch_summary.csv per site and flow.
4. Prints each upcoming storm with every model's newest total over the storm window, and saves it to
   data/reports/storms_latest.txt.
"""
from datetime import datetime, timedelta, timezone

import pandas as pd

from cottonwoods import storms, timefmt, verify
from cottonwoods.report import render_storms, render_verified
from cottonwoods.archive.forecasts import load_forecasts
from cottonwoods.archive.obs import load_hand_obs, load_rap, load_snotel, load_soundings, load_storm_totals
from cottonwoods.obs.sounding import summarize
from cottonwoods.config import PROCESSED_DIR, REPORTS_DIR, load_config

cfg = load_config()
now = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
# Runs from the last 20 days cover any storm still being tracked; obs from the last 25 days cover its window.
fc = load_forecasts(cfg, start=now - timedelta(days=20))
fc_raw = load_forecasts(cfg, start=now - timedelta(days=20), raw=True)   # unmodified copies: scored, not reported
obs = load_snotel(start=now - timedelta(days=25))
if fc.empty:
    raise SystemExit("No archived model runs in the last 20 days.")

snd = load_soundings(start=now - timedelta(days=25))
table = storms.run(fc, obs, cfg, now, soundings=summarize(snd) if not snd.empty else None,
                   rap=load_rap(start=now - timedelta(days=25)), fc_raw=fc_raw)

days_path = PROCESSED_DIR / "gauge_pillow_days.csv"
first = not days_path.exists()        # the first time: the whole archive; balloons only from the first SNOTEL day
d_obs = load_snotel() if first else obs
d_snd = load_soundings(start=d_obs["time_utc"].min() - timedelta(days=1)) if first and not d_obs.empty else snd
days = verify.gauge_pillow_days(d_obs, cfg, summarize(d_snd).set_index("time_utc") if not d_snd.empty else None)
days = verify.merge_gauge_pillow_days(days, days_path)
verify.undercatch_summary(days, cfg).to_csv(PROCESSED_DIR / "undercatch_summary.csv", index=False)
text = (f"Storms, {timefmt.now_line(now)}. Times are Mountain (MDT/MST), 24-hour.\n\n"
        + render_storms(table, fc, cfg, now, storms.point_steps(obs, cfg)))
scores, totals, readings = storms.load_scores(), load_storm_totals(), load_hand_obs()
binned = storms.bin_scores(table, scores, totals, readings, cfg)
storms.save_bin_scores(binned)
verified = render_verified(table, scores, totals, cfg, now, readings=readings, binned=binned,
                           block_scores=storms.load_block_scores())
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
