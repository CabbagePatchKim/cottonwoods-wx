"""Which resorts benefit in which flow: each point's precip share by 700 mb regime, from SNOTEL + SLC soundings.

    python scripts/regime_factors.py                      # Nov-Apr 2010 to now (re-run each spring)

For every wet day (mean of the reference SNOTEL sites >= 0.3"), the regime is the vector-mean 700 mb wind of the
12z and 00z Salt Lake City balloons that bracket the SNOTEL day, plus cold/warm from the 700 mb temperature. A
point's factor in a regime = its share of precip on those days / its share on all wet days. 1.10 means the resort
gets 10% more than usual in that flow. Gauge siting biases cancel out, because each site is compared to itself.

Writes data/processed/regime_factors.csv (point, flow, thermal, band, factor, n_days): flow-only rows (thermal and
band ""), flow + 2-way warm/cold rows, and flow + 3-way band rows (config temp_bands_700mb_c). Reports use the finest
level with enough wet days (regimes.factor_for).
Needs the balloon archive (scripts/archive_soundings.py --start 2010-11-01) and downloads SNOTEL daily totals.
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from cottonwoods import regimes
from cottonwoods.archive.obs import load_soundings
from cottonwoods.config import PROCESSED_DIR, load_config
from cottonwoods.obs import snotel
from cottonwoods.obs.sounding import summarize

cfg = load_config()
rcfg = cfg["regimes"]
sites = rcfg["factor_sites"]                                  # point -> SNOTEL site
trips = {cfg["snotel"][s]["triplet"]: s for s in set(sites.values())}
now = datetime.now(timezone.utc)

# Daily SNOTEL gauge totals, one cool season per request
frames = []
for y in range(2010, now.year + (1 if now.month >= 11 else 0)):
    frames.append(snotel.fetch(list(trips), f"{y}-11-01", f"{y + 1}-04-30", elements=("PREC",), duration="DAILY"))
d = pd.concat(frames)
d["site"] = d["station"].map(trips)
p = d.pivot_table(index="time", columns="site", values="value")
inc = p.diff().shift(-1).clip(lower=0)                       # precip during day D = total at D+1 minus total at D
inc = inc[inc.index.month.isin([11, 12, 1, 2, 3, 4])]

# Regime per SNOTEL day (local midnight to midnight): balloons at 12z D (5 am MST) and 00z D+1 (5 pm MST)
snd = summarize(load_soundings(start="2010-10-01")).set_index("time_utc")
u = -snd["wspd700_kt"] * np.sin(np.radians(snd["wdir700"]))
v = -snd["wspd700_kt"] * np.cos(np.radians(snd["wdir700"]))
rows = []
for day in inc.index:
    t = [day + pd.Timedelta(hours=12), day + pd.Timedelta(hours=24)]
    uu, vv, tt = u.reindex(t), v.reindex(t), snd["t700_c"].reindex(t)
    if uu.notna().any():
        d700 = float(regimes.wind_dir_deg(uu.mean(), vv.mean()))
        rows.append({"day": day, "flow": regimes.flow_label(d700, rcfg), "thermal": regimes.thermal_label(tt.mean(), rcfg),
                     "band": regimes.band_label(tt.mean(), rcfg)})
x = inc.join(pd.DataFrame(rows).set_index("day"), how="inner")

# Reference = mean of the sites with a long record (Atwater only starts in 2021; it's compared on its own days)
long_sites = [s for s in set(sites.values()) if inc[s].notna().mean() > 0.8]
x["ref"] = x[long_sites].mean(axis=1)
x = x[(x["ref"] >= 0.3) & x[long_sites].notna().all(axis=1)]

out = []
for point, site in sites.items():
    xs = x[x[site].notna()]
    base = xs[site].sum() / xs["ref"].sum()                  # the site's share on all its wet days
    levels = [(flow, "", "", g) for flow, g in xs.groupby("flow")]
    levels += [(flow, th, "", g) for (flow, th), g in xs.groupby(["flow", "thermal"])]
    levels += [(flow, "", bd, g) for (flow, bd), g in xs.groupby(["flow", "band"])]
    for flow, thermal, band, g in levels:
        out.append({"point": point, "site": site, "flow": flow, "thermal": thermal, "band": band,
                    "factor": round((g[site].sum() / g["ref"].sum()) / base, 3), "n_days": len(g)})
res = pd.DataFrame(out)
path = PROCESSED_DIR / "regime_factors.csv"
res.to_csv(path, index=False)
pd.set_option("display.width", 160)
flow_only = res[(res.thermal == "") & (res.band == "")]
print(flow_only.pivot_table(index="flow", columns="point", values="factor").round(2).to_string())
bands = res[res.band != ""]
print("\nflow + band: factor (wet days); used where days >=", rcfg["factor_min_days"])
print(bands.assign(cell=bands.factor.map("{:.2f}".format) + " (" + bands.n_days.astype(str) + ")")
      .pivot_table(index=["flow", "band"], columns="point", values="cell", aggfunc="first").to_string())
print(f"\nwet days: {len(x)} ({x.index.min():%Y-%m} to {x.index.max():%Y-%m}); wrote {path}")
