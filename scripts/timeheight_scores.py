"""Time-height measurement (owner 2026-11-19): which conditions bring heavy observed snow at Alta, and where the
models' water goes wrong. Laptop only; needs scripts/backfill_runs.py + backfill_timeheight.py for each winter.

    python scripts/timeheight_scores.py --winters 2025-26 2024-25 [--lead 24]

Each storm (backfill storm list, >= backfill.min_water_in) is cut into timeheight.block_hours blocks. Per block:
observed SNOTEL gauge water (the storm list's gauge), the RAP-analysis features (an_*), and each backfilled run's
water (the run backfill.plan_runs picked at --lead) with its own BUFKIT features (fc_*, HRRR / GFS).
Writes data/local/backfill/timeheight_blocks.csv and prints:
  A. observed: wet blocks by analysis conditions (mean observed water per block, share of heavy blocks)
  B. models: water ratio (model / observed, summed) by the same analysis conditions, and by the run's own forecast
     conditions (what a correction could use).
Thresholds are config timeheight's first guesses: re-run after changing them (no new downloads needed).
"""
import argparse

import numpy as np
import pandas as pd

from cottonwoods import backfill, storms, timeheight
from cottonwoods.archive.forecasts import load_forecasts
from cottonwoods.archive.obs import load_snotel
from cottonwoods.config import DATA_DIR, load_config
from cottonwoods.models import bufkit
from cottonwoods.units import MM_PER_IN
from cottonwoods.windows import window_total

ap = argparse.ArgumentParser()
ap.add_argument("--winters", nargs="+", required=True)
ap.add_argument("--lead", type=int, default=24)
args = ap.parse_args()
pd.set_option("display.width", 220)

cfg = load_config()
tc = cfg["timeheight"]
base = DATA_DIR / "local" / "backfill"
cache = DATA_DIR / "local" / "bufkit"
point = cfg["storms"]["point"]
all_storms = pd.read_csv(base / "storms.csv", parse_dates=["final_start", "final_end"])
rows, onsets, spill = [], [], []
for w in args.winters:
    y = int(w[:4])
    found = all_storms[(all_storms["final_start"] >= f"{y}-10-01") & (all_storms["final_start"] < f"{y + 1}-06-01")]
    found = found[found["obs_water_in"] >= cfg["backfill"]["min_water_in"]]
    lv_path = base / "timeheight" / f"analysis_{w}_levels.parquet"
    if found.empty or not lv_path.exists():
        print(f"[th] {w}: no storms or no analyses yet (run backfill_timeheight.py)")
        continue
    an_lv, an_hr = pd.read_parquet(lv_path), pd.read_parquet(base / "timeheight" / f"analysis_{w}_hours.parquet")
    start, end = found["final_start"].min() - pd.Timedelta(days=4), found["final_end"].max()
    fc = load_forecasts(cfg, start=start, end=end, root=base / "archive")
    fc = fc[fc["point"] == point]
    sn = load_snotel(start - pd.Timedelta(days=1), end + pd.Timedelta(days=1))
    gsteps, _ = backfill.point_gauge_steps(sn, cfg, point)
    sides = {k: storms.obs_steps(sn, v, cfg["gauge"]) for k, v in tc["spill_sites"].items()}
    for st in found.itertuples():
        pad = pd.Timedelta(hours=tc["pad_hours"])
        sel = (an_lv["time"] >= st.final_start - pad) & (an_lv["time"] <= st.final_end)
        an = (timeheight.features(an_lv[sel], an_hr[an_hr["time"].isin(an_lv.loc[sel, "time"])], cfg)
              if sel.any() else pd.DataFrame(columns=["time"]))
        runs = []
        for model, lead, init in backfill.runs_by_lead(pd.Series(st._asdict()), cfg, leads=[args.lead]):
            feats = None
            path = cache / model / f"alta_{init:%Y%m%d%H}.buf"
            if model in tc["backfill_models"] and path.exists():
                feats = timeheight.features(*bufkit.parse(path.read_text()), cfg)
            runs.append({"model": model, "lead": lead, "init": init, "feats": feats,
                         "steps": fc[(fc["model"] == model) & (fc["init"] == init)]})
        for r in timeheight.storm_block_rows(st, cfg, gsteps, an, runs, sides):
            rows.append({"winter": w, **r})
        for r in runs:
            onsets.append({"winter": w, **timeheight.onset_row(st, cfg, gsteps, an, r)})
        # storm-total spillover, gauges AND pillows (gauges undercatch in wind)
        sp = {"winter": w, "storm_id": st.storm_id, **timeheight.block_features(an, st.final_start, st.final_end, "an_")}
        for k, sites in tc["spill_sites"].items():
            sp[f"{k}_gauge_in"] = window_total(sides[k], st.final_start, st.final_end) / MM_PER_IN
            sp[f"{k}_pillow_in"] = pd.Series([timeheight.pillow_gain(sn, s, st.final_start, st.final_end) for s in sites]).mean()
        spill.append(sp)
df = pd.DataFrame(rows)
out = base / "timeheight_blocks.csv"
df.to_csv(out, index=False)
on = pd.DataFrame(onsets)
on.to_csv(base / "timeheight_onsets.csv", index=False)
spill = pd.DataFrame(spill)
spill.to_csv(base / "timeheight_spillover.csv", index=False)

obs = df.drop_duplicates(["storm_id", "block_start"])
wet = obs[obs["obs_in"] >= tc["wet_block_in"]]
heavy = np.percentile(wet["obs_in"], 75) if len(wet) else np.nan
print(f"[th] {', '.join(args.winters)}: {obs['storm_id'].nunique()} storms, {len(obs)} blocks of {tc['block_hours']} h, "
      f"{len(wet)} wet (>= {tc['wet_block_in']}\"); heavy = top quarter of wet blocks (>= {heavy:.2f}\") -> {out.name}")


def bins_of(d: pd.DataFrame, pre: str) -> dict[str, pd.Series]:
    """The condition groupings, from the an_ (analysis) or fc_ (forecast) features."""
    sector = pd.cut(d[pre + "dir_front"], [0, 45, 135, 225, 270, 315, 360.1], right=False,
                    labels=["N-NE", "E-SE", "S-SW", "W", "NW", "N"])
    return {
        "convective hours": pd.cut(d[pre + "conv_share"], [-0.01, 0, 0.5, 1.0], labels=["none", "some", "most"]),
        "moist depth km (mean)": pd.cut(d[pre + "moist_depth_mean"] / 1000, [-0.01, 1, 2, 3, 4, 20]),
        "theta-e drop K (max)": pd.cut(d[pre + "thetae_drop_max"].fillna(0), [-99, 0, 2, 4, 99]),
        "growth zone saturated m": pd.cut(d[pre + "dgz_sat_mean"], [-1, 0, 500, 1000, 5000]),
        "650 mb wind": sector,
        "front in block": d[pre + "front"].map({True: "yes", False: "no"}),
        "dry gap under cloud m": pd.cut(d[pre + "subcloud_dry_mean"].fillna(0), [-1, 0, 250, 750, 99999]),
        "cross-barrier wind m/s": pd.cut(d[pre + "ucross_mean"], [-99, 5, 10, 15, 20, 99]),
        "Froude (median)": pd.cut(d[pre + "froude_median"], [-0.01, 0.5, 1.0, 98, 100],
                                  labels=["<0.5 blocked", "0.5-1", ">1 over", "unstable"]),
        "drift km": pd.cut(d[pre + "drift_mean"], [-0.01, 5, 10, 20, 999]),
    }


print("\n=== A. Observed: wet blocks by ANALYSIS (RAP) conditions")
for name, g in bins_of(wet, "an_").items():
    t = wet.groupby(g, observed=True)["obs_in"].agg(blocks="size", mean_in="mean",
                                                    heavy_share=lambda s: (s >= heavy).mean())
    print(f"\n{name}\n{t.round(2).to_string()}")

runs = df[df["model"].notna() & df["obs_in"].ge(tc["wet_block_in"])]
for pre, label in (("an_", "ANALYSIS (what happened)"), ("fc_", "the run's OWN FORECAST (usable for a correction)")):
    print(f"\n=== B. Model water / observed in wet blocks, lead {args.lead} h, by {label}")
    d = runs if pre == "an_" else runs[runs["fc_hours"].notna()] if "fc_hours" in runs else runs.iloc[0:0]
    for name, g in bins_of(d, pre).items():
        t = d.groupby([g, d["model"]], observed=True).apply(
            lambda x: f"{x['fc_in'].sum() / x['obs_in'].sum():.2f} ({len(x)})", include_groups=False).unstack("model")
        print(f"\n{name}  (ratio, (blocks))\n{t.to_string()}")


# ---- overrunning: onset timing
print(f"\n=== C. Onset (first {tc['onset_in']}\" of water): model minus observed, hours (+ = model EARLY), "
      f"lead {args.lead} h")
o = on.dropna(subset=["onset_err_h"])
for pre, label in (("an_", "dry gap really there before the observed onset"),
                   ("fc_", "dry gap in the run's own forecast before its onset")):
    if pre + "subcloud_dry_mean" not in o:
        continue
    g = pd.cut(o[pre + "subcloud_dry_mean"].fillna(0), [-1, 0, 250, 750, 99999])
    t = o.groupby([g, o["model"]], observed=True)["onset_err_h"].apply(
        lambda s: f"{s.median():+.1f} ({len(s)})").unstack("model")
    print(f"\nby {label} (m; median error, (storms))\n{t.to_string()}")

# ---- spillover: back (Thaynes) vs front (Brighton)
print("\n=== D. Spillover: back side (Thaynes Canyon) / front (Brighton), observed")
bl = obs[obs["obs_front_in"] >= tc["wet_block_in"]] if "obs_front_in" in obs else obs.iloc[0:0]
for name in ("cross-barrier wind m/s", "Froude (median)", "drift km", "650 mb wind"):
    g = bins_of(bl, "an_")[name]
    t = bl.groupby(g, observed=True).apply(lambda x: pd.Series({
        "blocks": len(x), "back/front": x["obs_back_in"].sum() / x["obs_front_in"].sum()}), include_groups=False)
    print(f"\n6 h blocks by {name} (gauges)\n{t.round(2).to_string()}")
s = spill[(spill["front_gauge_in"] >= 0.3)]
for name in ("cross-barrier wind m/s", "Froude (median)"):
    g = bins_of(s, "an_")[name]
    t = s.groupby(g, observed=True).apply(lambda x: pd.Series({
        "storms": len(x), "gauge back/front": x["back_gauge_in"].sum() / x["front_gauge_in"].sum(),
        "pillow back/front": x["back_pillow_in"].sum() / x["front_pillow_in"].sum()}), include_groups=False)
    print(f"\nstorm totals by {name} (gauge and pillow: undercatch check)\n{t.round(2).to_string()}")
