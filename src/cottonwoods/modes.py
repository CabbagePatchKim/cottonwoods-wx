"""Precip mode per model step: frontal / post-frontal / cellular / lake-effect (thresholds in config regimes:).

Every model step also gets a precip MODE, which matters for snow ratio and for where the snow falls:
  frontal      within front_window_hours of the front, and stable steps before it (warm-advection snow)
  post-frontal after the front, stable
  cellular     unstable (CAPE or a steep 700-500 mb lapse rate), outside the frontal window
  lake-effect  post-frontal + lake much warmer than 700 mb + flow off the Great Salt Lake + moist low levels
The front comes from each run's own time series at the point, so a model that's late with the front is late here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Needs the per-step flow columns from regimes.add_step_regimes (flow_dir700); run that first.


def find_front(g, cfg: dict):
    """Time of the front in one run at one point (sorted steps), or None: the end of the largest 6 h drop in
    700 mb temperature, if it's at least front_drop_c and the 700 mb wind veers at least front_veer_deg."""
    if "t_700_k" not in g or g["t_700_k"].notna().sum() < 3:
        return None
    s = g.set_index("valid_end").sort_index()
    t = s["t_700_k"].astype(float)
    best, when = 0.0, None
    for end in t.index:
        before = t[(t.index >= end - pd.Timedelta(hours=6)) & (t.index < end)]
        if before.empty:
            continue
        drop = float(before.max() - t[end])
        if drop > best:
            start = before.idxmax()
            veer = (float(s.at[end, "flow_dir700"]) - float(s.at[start, "flow_dir700"])) % 360 if "flow_dir700" in s else 0
            if drop >= cfg["front_drop_c"] and 0 < veer <= 180 and veer >= cfg["front_veer_deg"]:
                best, when = drop, end
    return when


def _lapse_700_500(row) -> float:
    """700-500 mb lapse rate (C per km) from temperatures and heights (heights filled hypsometrically if needed)."""
    from .profile import level_heights
    t7, t5 = row.get("t_700_k", np.nan), row.get("t_500_k", np.nan)
    z = level_heights([700, 500], {700: t7, 500: t5}, {700: row.get("z_700_m", np.nan), 500: row.get("z_500_m", np.nan)})
    if not (np.isfinite(t7) and np.isfinite(t5)) or 700 not in z or 500 not in z or z[500] <= z[700]:
        return np.nan
    return (t7 - t5) / ((z[500] - z[700]) / 1000.0)


def add_precip_modes(df, cfg: dict):
    """Add front_time, lapse_700_500, lake_delta_c and precip_mode to every step of one run (all its points)."""
    if not {"t_700_k", "flow_dir700"} <= set(df.columns):
        return df
    out = df.copy()
    win = pd.Timedelta(hours=cfg["front_window_hours"])
    lake_by_month = {int(k): v for k, v in cfg["lake_temp_c_by_month"].items()}
    lo, hi = cfg["lake_flow_deg"]
    out["lapse_700_500"] = [_lapse_700_500(r) for r in out.to_dict("records")]
    lake = out["valid_end"].dt.month.map(lake_by_month).astype(float)
    out["lake_delta_c"] = lake - (out["t_700_k"] - 273.15)
    out["front_time"] = pd.NaT
    out["precip_mode"] = None
    wet_mm, gap = cfg["event_wet_mm"], pd.Timedelta(hours=cfg["event_gap_hours"])
    for point, g in out.groupby("point"):
        g = g.sort_values("valid_end")
        # Precip events at this point (wet steps less than `gap` apart, as in the storm tracker). The front is
        # searched for within each event (+-6 h), so an earlier system's front can't label this storm.
        wet = g[g["qpf_mm"] >= wet_mm]
        events, cur = [], None
        for _, r in wet.iterrows():
            if cur and r.valid_start - cur[1] < gap:
                cur[1] = r.valid_end
            else:
                if cur:
                    events.append(cur)
                cur = [r.valid_start, r.valid_end]
        if cur:
            events.append(cur)
        for ev_start, ev_end in events:
            pad = pd.Timedelta(hours=6)
            ev = g[(g["valid_end"] > ev_start - pad) & (g["valid_start"] < ev_end + pad)]
            front = find_front(ev, cfg)
            for i, r in ev.iterrows():
                unstable = (np.nan_to_num(r.get("cape_jkg", np.nan)) >= cfg["cellular_cape_jkg"]
                            or np.nan_to_num(r["lapse_700_500"]) >= cfg["cellular_lapse_c_per_km"])
                near_front = front is not None and abs(r.valid_end - front) <= win
                after = front is not None and r.valid_end > front + win
                lake_ok = (after and np.isfinite(r["lake_delta_c"]) and r["lake_delta_c"] >= cfg["lake_delta_c"]
                           and lo <= r["flow_dir700"] <= hi
                           and np.nan_to_num(r.get("rh_700", np.nan)) >= cfg["lake_min_rh700"])
                if near_front:
                    mode = "frontal"
                elif lake_ok:
                    mode = "lake-effect"
                elif unstable:
                    mode = "cellular"
                elif after:
                    mode = "post-frontal"
                else:
                    mode = "frontal"  # stable, ahead of the front (or no front): stratiform / warm-advection snow
                out.at[i, "precip_mode"] = mode
                out.at[i, "front_time"] = front
    return out


def mode_string(steps, weight_col: str = "qpf_mm") -> str:
    """'frontal 55% -> post-frontal 30% -> lake-effect 15%' for wet steps in time order (consecutive runs joined)."""
    s = steps[steps["precip_mode"].notna()].sort_values("valid_start")
    if s.empty:
        return ""
    total = s[weight_col].clip(lower=0).sum() or 1.0
    parts = []
    for _, r in s.iterrows():
        w = max(float(r[weight_col]), 0.0)
        if parts and parts[-1][0] == r.precip_mode:
            parts[-1][1] += w
        else:
            parts.append([r.precip_mode, w])
    parts = [p for p in parts if p[1] / total >= 0.05]  # drop slivers under 5% of the water
    merged = []
    for m, w in parts:
        if merged and merged[-1][0] == m:
            merged[-1][1] += w
        else:
            merged.append([m, w])
    return " -> ".join(f"{m} {100 * w / total:.0f}%" for m, w in merged)
