"""Storm timeline and run-to-run trends, for the HTML storm report (owner, 2026-10-27 plan; built 2026-10-29).

Per deterministic run at a point, over the storm window (config trends):
  onset   start of the first rate_hours window whose average snow rate reaches onset_rate_in_hr (snow, not precip)
  peak    the rate_hours window with the highest average rate: its start and in/h (a rate_hours average, so 1 h and
          3 h models compare fairly)
  end     end of the last window still at onset_rate_in_hr
Trends compare a model's newest run with its previous one (onset / peak / end hours earlier or later, snow and water
up or down) and look at its last `runs` runs for convergence (steady, up, down, or mixed).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .report import _snow_col, model_rank
from .units import MM_PER_IN
from .windows import window_total, within_lead


def hourly(steps: pd.DataFrame, col: str, s, e) -> pd.Series:
    """`col` per hour from s to e (index = hour start), each step's amount spread evenly over its hours."""
    hours = pd.date_range(s, e, freq="h", inclusive="left")
    out = pd.Series(0.0, index=hours)
    for vs, ve, v in zip(steps["valid_start"], steps["valid_end"], steps[col]):
        n = (ve - vs) / pd.Timedelta(hours=1)
        if n <= 0 or pd.isna(v):
            continue
        sel = (hours >= vs) & (hours < ve)
        out[sel] += v / n
    return out


def timeline(steps: pd.DataFrame, s, e, cfg: dict) -> dict:
    """One run at one point over [s, e): onset, end, peak_start, peak_rate (in/h), snow (in), water (in); times None
    when no window reaches the onset rate."""
    tc = cfg["trends"]
    col = _snow_col(steps)
    rate = hourly(steps, col, s, e).rolling(tc["rate_hours"]).mean().shift(-(tc["rate_hours"] - 1)).dropna()
    snowy = rate[rate >= tc["onset_rate_in_hr"]]
    peak = rate.idxmax() if len(rate) and rate.max() > 0 else None
    span = pd.Timedelta(hours=tc["rate_hours"])
    return {"onset": snowy.index[0] if len(snowy) else None, "end": snowy.index[-1] + span if len(snowy) else None,
            "peak_start": peak, "peak_rate": float(rate.max()) if peak is not None else 0.0,
            "snow": window_total(steps, s, e, col), "water": window_total(steps, s, e) / MM_PER_IN}


def _hours(a, b):
    """b - a in hours (+ = later), or None."""
    return None if a is None or b is None else (b - a) / pd.Timedelta(hours=1)


def direction(snow: list[float], steady_in: float) -> str:
    """Oldest -> newest totals: 'steady' (every change under steady_in), 'up', 'down', or 'mixed'."""
    d = np.diff(snow)
    if len(d) == 0 or (np.abs(d) < steady_in).all():
        return "steady"
    big = d[np.abs(d) >= steady_in]
    return "up" if (big > 0).all() else "down" if (big < 0).all() else "mixed"


def model_trends(st, fc: pd.DataFrame, cfg: dict, point: str) -> list[dict]:
    """Per deterministic model (display order): its last trends.runs runs reaching the storm, newest first, each
    {init, full (covers the whole storm), timeline}; change (newest vs previous, both full): onset_h, peak_h, end_h
    (+ = later), snow_in, water_in; snow_path (full runs' snow, oldest -> newest) and direction."""
    tc, s, e = cfg["trends"], st.fc_start, st.fc_end
    fp = fc[fc["point"] == point]
    fp = fp[fp["member"].isna()] if "member" in fp.columns else fp
    out = []
    for model, g in sorted(fp.groupby("model"), key=lambda mg: model_rank(mg[0])):
        runs = []
        for init in sorted(g["init"].unique(), reverse=True):
            r = within_lead(g[g["init"] == init], cfg["storms"]["max_lead_hours"])
            if r.empty or r["valid_end"].max() <= s:
                continue
            runs.append({"init": pd.Timestamp(init), "full": r["valid_start"].min() <= s and r["valid_end"].max() >= e,
                         "timeline": timeline(r, s, e, cfg)})
            if len(runs) == tc["runs"]:
                break
        if not runs:
            continue
        full = [r for r in runs if r["full"]]
        change = None
        if len(full) >= 2 and full[0] is runs[0]:
            a, b = full[1]["timeline"], full[0]["timeline"]
            change = {"onset_h": _hours(a["onset"], b["onset"]), "peak_h": _hours(a["peak_start"], b["peak_start"]),
                      "end_h": _hours(a["end"], b["end"]), "snow_in": b["snow"] - a["snow"],
                      "water_in": b["water"] - a["water"]}
        path = [r["timeline"]["snow"] for r in reversed(full)]
        out.append({"model": model, "runs": runs, "change": change, "snow_path": path,
                    "direction": direction(path, tc["steady_in"]) if len(path) >= 2 else ""})
    return out


def ensemble_trends(st, fc: pd.DataFrame, cfg: dict, point: str) -> list[dict]:
    """Per ensemble model: its last trends.runs runs reaching the storm, newest first: init, wet (share of members with
    >= verify.event_swe_in), mean_water_in."""
    tc, s, e = cfg["trends"], st.fc_start, st.fc_end
    thr = cfg["verify"]["event_swe_in"]
    fp = fc[(fc["point"] == point)]
    fp = fp[fp["member"].notna()] if "member" in fp.columns else fp.iloc[0:0]
    out = []
    for model, g in sorted(fp.groupby("model"), key=lambda mg: model_rank(mg[0])):
        runs = []
        for init in sorted(g["init"].unique(), reverse=True)[:tc["runs"]]:
            r = within_lead(g[g["init"] == init], cfg["storms"]["max_lead_hours"])
            if r.empty or r["valid_end"].max() <= s:
                continue
            per = r.groupby("member").apply(lambda m: window_total(m, s, e), include_groups=False) / MM_PER_IN
            runs.append({"init": pd.Timestamp(init), "wet": float((per >= thr).mean()), "mean_water_in": float(per.mean())})
        if runs:
            out.append({"model": model, "runs": runs})
    return out
