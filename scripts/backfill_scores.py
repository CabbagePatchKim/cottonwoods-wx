"""Verification backfill step 4 (docs/PLAN.md step 4): score the backfilled runs (data/local/backfill/archive) on the
storm list (data/local/backfill/storms.csv), one winter, every model fetched, at each of backfill.leads_hours.
Laptop only; writes data/local/backfill/scores_<winter>.csv and prints the season table.

    python scripts/backfill_scores.py --winter 2025-26

Each storm's run at a lead is the one backfill.plan_runs picked for it (the newest run issued at least that long
before the storm began). Totals use the live code (windows.window_total over the storm's final window, bins.bin_score
with config verify). Observed water: the storm list's SNOTEL gauge total. Observed snow: the owner's journal stake
reading where there is one, else the NWS statement (the NWS 3/5/2026 period spans two storms: 33" vs the stake's 16").

SHORT RUNS: backfill_runs.py fetched each run to the storm's exact last hour, so a 3-hourly run can stop 1-2 h
before a storm ending off a step edge. A run ending at most MAX_SHORT_H before the end is scored over the window it
covers, with observed water re-totalled from the SNOTEL gauge over that same window (cut_h = hours left out).
Observed snow stays the whole storm's total (no hourly snow before the Collins Worker).
"""
import argparse

import numpy as np
import pandas as pd

from cottonwoods import backfill, bins
from cottonwoods.archive.forecasts import load_forecasts
from cottonwoods.archive.obs import load_snotel
from cottonwoods.config import DATA_DIR, load_config
from cottonwoods.units import MM_PER_IN
from cottonwoods.windows import window_total

ap = argparse.ArgumentParser()
ap.add_argument("--winter", required=True, help="e.g. 2025-26 (October to May)")
args = ap.parse_args()

cfg = load_config()
base = DATA_DIR / "local" / "backfill"
y = int(args.winter[:4])
found = pd.read_csv(base / "storms.csv", parse_dates=["final_start", "final_end"])
found = found[(found["final_start"] >= f"{y}-10-01") & (found["final_start"] < f"{y + 1}-06-01")]
found = found[found["obs_water_in"] >= cfg["backfill"]["min_water_in"]]
point = cfg["storms"]["point"]
MAX_SHORT_H = 6                          # one model step (GFS 3 h; IFS 6 h past 144 h)


start, end = found["final_start"].min() - pd.Timedelta(days=4), found["final_end"].max()
fc = pd.concat([load_forecasts(cfg, start=start, end=end, root=base / "archive", raw=raw) for raw in (False, True)],
               ignore_index=True)
fc = fc[fc["point"] == point]
thr = cfg["verify"]["event_swe_in"]
gsteps, gauge = backfill.point_gauge_steps(load_snotel(start - pd.Timedelta(days=1), end + pd.Timedelta(days=1)), cfg, point)

rows = []
for _, st in found.iterrows():
    s, e = st.final_start, st.final_end
    if pd.notna(st.journal_snow_in):
        obs_snow, snow_src = float(st.journal_snow_in), "journal"
    elif pd.notna(st.nws_snow_in):
        obs_snow, snow_src = float(st.nws_snow_in), "nws"
    else:
        obs_snow, snow_src = np.nan, ""
    for model, lead, init in backfill.runs_by_lead(st, cfg):
        for label in (model, model + " raw"):
            g = fc[(fc["model"] == label) & (fc["init"] == init)]
            if g.empty:
                continue
            cut = min(e, g["valid_end"].max())
            if (e - cut) / pd.Timedelta(hours=1) > MAX_SHORT_H or g["valid_start"].min() > s:
                rows.append({"storm_id": st.storm_id, "model": label, "lead_h": lead, "init": init, "status": "short"})
                continue
            obs_water = st.obs_water_in if cut == e else window_total(gsteps, s, cut) / MM_PER_IN
            water = window_total(g, s, cut) / MM_PER_IN
            snow = window_total(g, s, cut, "snow_in") if g["snow_in"].notna().any() else np.nan
            r = {"storm_id": st.storm_id, "model": label, "lead_h": lead, "init": init, "status": "ok",
                 "cut_h": (e - cut) / pd.Timedelta(hours=1),
                 "fc_water_in": water, "obs_water_in": obs_water, "water_err": water - obs_water,
                 "fc_snow_in": snow, "obs_snow_in": obs_snow, "obs_snow_src": snow_src,
                 "snow_err": snow - obs_snow if pd.notna(obs_snow) else np.nan}
            if pd.notna(obs_snow) and pd.notna(snow):
                r.update(bins.bin_score(snow, obs_snow, cfg["verify"], water >= thr, obs_water >= thr))
            rows.append(r)

sc = pd.DataFrame(rows)
out = base / f"scores_{args.winter}.csv"
sc.to_csv(out, index=False)
ok = sc[sc["status"] == "ok"]
print(f"[scores] {args.winter}: {len(found)} storms (>= {cfg['backfill']['min_water_in']}\" water), "
      f"{ok['obs_snow_in'].notna().groupby(ok['storm_id']).any().sum()} with an observed snow total; "
      f"{len(ok)} scored runs ({(ok['cut_h'] > 0).sum()} cut short <= {MAX_SHORT_H} h), "
      f"{(sc['status'] == 'short').sum()} too short; gauge {gauge} -> {out}")


def table(d: pd.DataFrame) -> pd.DataFrame:
    snowy = d[d["points"].notna()] if "points" in d else d.iloc[0:0]
    return pd.Series({
        "storms": d["storm_id"].nunique(),
        "water bias": d["water_err"].mean(), "water MAE": d["water_err"].abs().mean(),
        "water ratio": d["fc_water_in"].sum() / d["obs_water_in"].sum(),
        "snow n": len(snowy), "snow bias": snowy["snow_err"].mean(), "snow MAE": snowy["snow_err"].abs().mean(),
        "bin avg": snowy["points"].mean(), "right bins": f"{int(snowy['hit'].sum())}/{len(snowy)}" if len(snowy) else "-",
    })


pd.set_option("display.width", 200)
pd.set_option("display.float_format", lambda v: f"{v:.2f}")
print("\n=== By model and lead (water vs SNOTEL gauge; snow vs journal stake, else NWS)")
print(ok.groupby(["model", "lead_h"]).apply(table, include_groups=False).to_string())
print("\n=== All leads together")
print(ok.groupby("model").apply(table, include_groups=False).to_string())
both = ok.pivot_table(index=["storm_id", "lead_h"], columns="model", values="water_err")
if {"GFS", "IFS"} <= set(both.columns):
    both = both[["GFS", "IFS"]].dropna()           # only these two (HRRR / U of U runs cover fewer storms)
    closer = (both["GFS"].abs() < both["IFS"].abs()).sum()
    print(f"\nSame storm and lead, water: GFS closer {closer} of {len(both)}, IFS closer {len(both) - closer - (both['GFS'].abs() == both['IFS'].abs()).sum()}")
print("\n=== Per storm (24 h lead): water fc / obs, snow fc / obs")
w = ok[ok["lead_h"] == 24].pivot_table(index="storm_id", columns="model", values=["fc_water_in", "fc_snow_in"])
w.columns = [f"{m} {'water' if c == 'fc_water_in' else 'snow'}" for c, m in w.columns]
obs = found.set_index("storm_id")[["obs_water_in"]].join(
    ok.drop_duplicates("storm_id").set_index("storm_id")[["obs_snow_in", "obs_snow_src"]])
print(obs.join(w[sorted(w.columns)]).to_string())
