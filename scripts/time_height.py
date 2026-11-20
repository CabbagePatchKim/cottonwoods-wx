"""Time-height section of one model run at Alta (or KSLC) from its BUFKIT point sounding (owner, 2026-11-19;
models/bufkit.py, timeheight.py, config timeheight). Laptop, on demand; not part of the pass or the feed.

    python scripts/time_height.py --model HRRR --init 2026111812              # Alta, table + plot
    python scripts/time_height.py --model GFS --init 2025120400 --site kslc --every 6

Prints one line per hour (every --every hours) and writes data/local/timeheight/<model>_<site>_<init>.png and .csv.
Files are cached in data/local/bufkit. Thresholds are uncalibrated (see timeheight.py).
"""
import argparse
from datetime import datetime

from cottonwoods import timeheight
from cottonwoods.config import DATA_DIR, load_config
from cottonwoods.models import bufkit
from cottonwoods.timefmt import local
from cottonwoods.units import FT_PER_M

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True, help="HRRR, RAP, GFS or RRFS")
ap.add_argument("--init", required=True, help="YYYYMMDDHH (UTC)")
ap.add_argument("--site", default="alta")
ap.add_argument("--every", type=int, default=3, help="print every N hours")
args = ap.parse_args()

cfg = load_config()
init = datetime.strptime(args.init, "%Y%m%d%H")
levels, hours = bufkit.parse(bufkit.fetch(args.model, args.site, init, cfg, DATA_DIR / "local" / "bufkit"))
f = timeheight.features(levels, hours, cfg)
out = DATA_DIR / "local" / "timeheight"
out.mkdir(parents=True, exist_ok=True)
stem = out / f"{args.model.upper()}_{args.site}_{args.init}"
f.to_csv(stem.with_suffix(".csv"), index=False)
title = f"{args.model.upper()} {init:%Y-%m-%d %H}z at {args.site} (BUFKIT, model ground {f['ground_m'].iloc[0] * FT_PER_M:,.0f} ft)"
timeheight.plot(levels, f, title, stem.with_suffix(".png"))


def ft(m, below=False):
    return "  <gnd" if below else "     -" if m != m else f"{m * FT_PER_M / 1000:6.1f}"


print(title)
print("time (MT)    precip  frz kft  wbz kft  moist kft    depth  top C  DGZ sat  DGZ lift  thE drop  thE g-5  lapse  CAPE")
for r in f.iloc[::args.every].itertuples():
    moist = f"{ft(r.moist_base_m)}-{ft(r.moist_top_m).strip():>5}" if r.moist_depth_m > 0 else "         -   "
    flags = (" CONV" if r.convective else "") + (" FRONT" if r.front else "")
    print(f"{local(r.time):%a %d %H:%M}  {r.p01m / 25.4 if r.p01m == r.p01m else 0:5.2f}\"  {ft(r.fzl_m, r.fzl_below_ground)}"
          f"   {ft(r.wbz_m, r.wbz_below_ground)}   {moist}  {r.moist_depth_m * FT_PER_M:6.0f}ft  {r.moist_top_c:5.0f}"
          f"  {r.dgz_sat_m * FT_PER_M:5.0f}ft  {r.dgz_omega:7.1f}  {r.thetae_drop_k:7.1f}K  {r.thetae_sfc_top_k:6.1f}K"
          f"  {r.lapse_sfc_top:5.1f}  {r.cape if r.cape == r.cape else 0:4.0f}{flags}")
if f.attrs.get("front_at_end"):
    print("FRONT at the run's last hour: the cooling may continue past the run (front at or after it)")
print(f"-> {stem.with_suffix('.png')}  (+ .csv, every hour)")
