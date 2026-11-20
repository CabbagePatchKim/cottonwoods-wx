"""500 mb shortwaves and vorticity advection at Alta (owner 2026-11-19; shortwaves.py, config shortwaves). Laptop.

    python scripts/shortwaves.py --start 2026-11-15T12 --end 2026-11-19T00                 # "what happened"
    python scripts/shortwaves.py --start 2026-11-15T12 --end 2026-11-19T00 --init 2026111600   # + a forecast run

"What happened" = each archived GFS run's analysis_lead_hr (6 h) map, chained run to run. With --init, that run's own
maps (to 120 h) are analysed the same way and its shortwave passages at Alta are matched to the observed ones
(nearest in time): timing error (+ = forecast late) and strength error. Writes a map PNG of the tracks to
data/local/shortwaves/. Uses the pattern archive (data/archive/patterns; --root for another, e.g. the backfill's).
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from cottonwoods import shortwaves as sw
from cottonwoods.config import ARCHIVE_DIR, DATA_DIR, load_config
from cottonwoods.patterns import load_pattern
from cottonwoods.timefmt import local

ap = argparse.ArgumentParser()
ap.add_argument("--start", required=True)
ap.add_argument("--end", required=True)
ap.add_argument("--init", help="a forecast run to compare, YYYYMMDDHH")
ap.add_argument("--model", default="GFS")
ap.add_argument("--root", help="archive root holding patterns/ for the analyses (default the live archive)")
ap.add_argument("--fc-root", help="archive root for the --init run (default: --root)")
args = ap.parse_args()

cfg = load_config()
sc = cfg["shortwaves"]
root = Path(args.root) if args.root else None
pdir = (root or ARCHIVE_DIR) / "patterns" / f"model={args.model}"
start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
lat0, lon0 = cfg["points"]["alta"]["lat"], cfg["points"]["alta"]["lon"]
lead = sc["analysis_lead_hr"]
runs = []
for f in sorted(pdir.glob("init=*")):
    init = pd.Timestamp(f.stem.split("=")[1][:10][:8] + " " + f.stem.split("=")[1][8:10] + ":00")
    if start - pd.Timedelta(hours=lead) <= init <= end - pd.Timedelta(hours=lead):
        runs.append(load_pattern(args.model, init, cfg, root, grid=sc["grid"]))
an = sw.stitch_analyses(runs)
if an.empty:
    raise SystemExit(f"No {args.model} pattern runs between {start} and {end} in {pdir}")
times = sorted(an["time"].unique())
gaps = [f"{local(a):%a %d %H}-{local(b):%H}" for a, b in zip(times, times[1:]) if b - a > pd.Timedelta(hours=6)]
series, tracks, cross = sw.analyse(an, lat0, lon0, sc)
print(f"{args.model} analyses ({'/'.join(str(int(x)) for x in sorted(an['lead_hr'].unique()))} h maps chained): {len(times)} maps {local(times[0]):%a %d %H:%M} - "
      f"{local(times[-1]):%a %d %H:%M} MT" + (f"; GAPS {', '.join(gaps)}" if gaps else ""))
print(f"{tracks['track'].nunique() if len(tracks) else 0} shortwave tracks in the box; passages at Alta's longitude "
      f"(within {sc['passage_lat_deg']} deg):")
cross = cross.sort_values("time")
for c in cross.itertuples():
    side = "N" if c.dlat > 0 else "S"
    print(f"  {local(c.time):%a %d %H:%M} MT   {abs(c.dlat):.1f} deg {side} of Alta   strength {c.anom * 1e5:.1f}e-5/s"
          f"   moving {c.speed_kmh:.0f} km/h")
print("\ntime (MT)      PVA at Alta (1e-9/s2)   shortwave within "
      f"{sc['pva_radius_km']} km (1e-5/s)")
for r in series.itertuples():
    bar = "+" * int(max(0, r.pva * 1e9) // 2) or ("-" * int(max(0, -r.pva * 1e9) // 2))
    print(f"{local(r.time):%a %d %H:%M}     {r.pva * 1e9:7.1f}  {bar:<15}  {r.anom_near * 1e5:6.1f}")

fc_tracks, matched = None, []
if args.init:
    init = pd.Timestamp(f"{args.init[:8]} {args.init[8:10]}:00")
    fc = load_pattern(args.model, init, cfg, Path(args.fc_root) if args.fc_root else root, grid=sc["grid"])
    if fc.empty:
        raise SystemExit(f"No {args.model} pattern file for {args.init}")
    fc = fc.assign(time=fc["init"] + pd.to_timedelta(fc["lead_hr"], unit="h"))
    fs, fc_tracks, fcross = sw.analyse(fc, lat0, lon0, sc)
    print(f"\n{args.model} {init:%Y-%m-%d %H}z forecast: passages at Alta vs observed (closest in time AND latitude:"
          " hours/6 + degrees/2, within 24 h)")
    for c in fcross.sort_values("time").itertuples():
        if cross.empty:
            print(f"  {local(c.time):%a %d %H:%M} MT  (nothing observed to match)")
            continue
        dt = (cross["time"] - c.time).abs()
        o = cross.loc[(dt / pd.Timedelta(hours=6) + (cross["dlat"] - c.dlat).abs() / 2).idxmin()]
        if abs(o["time"] - c.time) > pd.Timedelta(hours=24):
            print(f"  {local(c.time):%a %d %H:%M} MT  strength {c.anom * 1e5:.1f}e-5/s: no observed trough within 24 h")
            continue
        err = (c.time - o["time"]) / pd.Timedelta(hours=1)
        matched.append(err)
        print(f"  {local(c.time):%a %d %H:%M} MT (lead {(c.time - init) / pd.Timedelta(hours=1):.0f} h) vs observed "
              f"{local(o['time']):%a %d %H:%M}: {err:+.1f} h ({'late' if err > 0 else 'early'}), strength "
              f"{c.anom * 1e5:.1f} vs {o['anom'] * 1e5:.1f}e-5/s, {c.dlat - o['dlat']:+.1f} deg lat")
    both = series.merge(fs, on="time", suffixes=("_obs", "_fc"))
    if len(both) > 2:
        print(f"  PVA at Alta, forecast vs observed over {len(both)} shared maps: r = "
              f"{np.corrcoef(both['pva_obs'], both['pva_fc'])[0, 1]:.2f}, peak "
              f"{local(both.loc[both['pva_fc'].idxmax(), 'time']):%a %d %H} vs "
              f"{local(both.loc[both['pva_obs'].idxmax(), 'time']):%a %d %H} MT")

import matplotlib  # noqa: E402 - only for the picture
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

lats, lons, vg = sw.grids(an, "vort500_s")
_, _, zg = sw.grids(an, "z500_m")
t_show = cross["time"].iloc[0] if len(cross) else times[len(times) // 2]
t_map = min(vg, key=lambda t: abs(t - t_show))
fig, ax = plt.subplots(figsize=(11, 7.5))
cf = ax.contourf(lons, lats, sw.anomaly(vg[t_map], lats, lons, sc["background_km"], sc["detail_km"]) * 1e5,
                 levels=np.arange(-20, 21, 2), cmap="RdBu_r", extend="both")
c = ax.contour(lons, lats, zg[t_map], levels=np.arange(4800, 6000, 60), colors="k", linewidths=0.8)
ax.clabel(c, fmt="%d", fontsize=7)
for tid, g in tracks.groupby("track"):
    ax.plot(g["lon"], g["lat"], "-o", color="purple", markersize=3)
    ax.annotate(f"{local(g['time'].iloc[-1]):%d/%H}", (g["lon"].iloc[-1], g["lat"].iloc[-1]), fontsize=7,
                color="purple")
if fc_tracks is not None:
    for tid, g in fc_tracks.groupby("track"):
        ax.plot(g["lon"], g["lat"], "--", color="darkorange", linewidth=1.2)
ax.plot(lon0, lat0, "k*", markersize=14)
ax.set_title(f"{args.model} 500 mb: shortwave vorticity anomaly (1e-5/s, colour), heights (m) at "
             f"{local(t_map):%a %d %H:%M} MT; tracks: purple = observed (6 h maps), orange dashed = run {args.init or '-'}",
             fontsize=9)
fig.colorbar(cf, ax=ax, fraction=0.03, label="vorticity minus background (1e-5/s)")
out = DATA_DIR / "local" / "shortwaves"
out.mkdir(parents=True, exist_ok=True)
png = out / f"{args.model}_{start:%Y%m%d%H}_{end:%Y%m%d%H}{'_' + args.init if args.init else ''}.png"
fig.savefig(png, dpi=90, bbox_inches="tight")
print(f"\n-> {png}")
