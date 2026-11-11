"""Verification backfill step 1 (docs/PLAN.md step 4): every past storm at the storm point, found in the archived
SNOTEL record by the live pass's rules, with its NWS statement and journal totals (src/cottonwoods/backfill.py).
Laptop, after `python scripts/backfill_obs.py snotel ...` and `... pns ...`:

    python scripts/backfill_storms.py

Writes data/local/backfill/storms.csv (local: not synced, not in git) and prints a per-winter summary.
"""
import pandas as pd

from cottonwoods import backfill
from cottonwoods.archive.obs import load_snotel, load_totals
from cottonwoods.config import DATA_DIR, load_config
from cottonwoods.journal import load_storms

cfg = load_config()
point = cfg["storms"]["point"]
found = backfill.attach_totals(backfill.observed_storms(load_snotel(), cfg, point), load_totals(), load_storms(), cfg)
out = DATA_DIR / "local" / "backfill"
out.mkdir(parents=True, exist_ok=True)
found.to_csv(out / "storms.csv", index=False)

found["winter"] = [f"{t.year - (t.month < 10)}-{(t.year - (t.month < 10) + 1) % 100:02d}" for t in found["final_start"]]
big = found["obs_water_in"] >= 0.5
summary = found.groupby("winter").agg(storms=("storm_id", "size"), gauge=("gauge", "first"),
                                      water=("obs_water_in", "sum"))
summary["water >= 0.5\""] = found[big].groupby("winter").size()
summary["NWS snow"] = found[found["nws_snow_in"].notna()].groupby("winter").size()
summary["NWS water only"] = found[found["nws_snow_in"].isna() & found["nws_water_in"].notna()].groupby("winter").size()
summary["journal"] = found[found["journal_snow_in"].notna()].groupby("winter").size()
pd.set_option("display.width", 200)
print(summary.fillna(0).astype({c: int for c in summary.columns if c not in ("gauge", "water")}).round({"water": 1}))
print(f"\n{len(found)} storms -> {out / 'storms.csv'}")
