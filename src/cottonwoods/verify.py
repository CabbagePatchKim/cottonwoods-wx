"""Verification: pair archived forecasts with observations over matching windows.

For every archived run and point, find the observation windows (default 24 h ending 12 UTC, which is
5 am MST) that the run fully covers, then compare:

  model QPF (inches liquid)  vs  observed liquid at each of the point's obs sites  <- tests the precip forecast
  model Kuchera snowfall     vs  hand-entered new snow (data/manual)                <- tests QPF x SLR together

Observed liquid comes from each site's precip GAUGE (owner, 2026-10-28): SNOTEL and Synoptic (Alta Collins) hourly
gauge precip through cottonwoods.gauge, or the daily precip total for NOAA coop stations (ALTA), which also report
snowfall. A SNOTEL snow PILLOW is the fallback (gauge missing, snowpack on the pillow) and the cross-check: every pair
keeps both, and undercatch_flag marks windows where the pillow rose well above the gauge (wind blowing snow past the
gauge). gauge_pillow_days / undercatch_summary collect that evidence per site and flow, from observations alone.
A point with several obs sites gets one row per site, and sites are scored separately. NOAA sites use windows ending at their observer's
time (ALTA: 6 pm MST = 01 UTC) instead of the shared window_end_utc_hour.

Windows the run does not fully cover, or that straddle a model step, are dropped rather than prorated.
Lead time is measured to the END of the window, so "lead 24-48" means the window ended 24-48 h after init.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import regimes
from .archive.obs import drop_unreliable_water
from .gauge import gauge_increments
from .units import MM_PER_IN


# Terms used below:
#   window   - a 24 h period ending at window_end_utc_hour (default 12 UTC = 5 am MST), or at a NOAA site's
#              observation time
#   lead     - hours from the model's init time to the END of the window (how far ahead it was forecast)
#   pairs    - one row per (model run, point, obs site, window): forecast next to observation
#   POD/FAR  - probability of detection (share of real events the model caught) and false alarm ratio
#              (share of the model's events that didn't happen)


def _series(obs: pd.DataFrame, site: str, element: str = "WTEQ") -> pd.Series:
    s = obs[(obs["site"] == site) & (obs["element"] == element)]
    if s.empty:
        return pd.Series(dtype=float)
    s = s.sort_values("time_utc").drop_duplicates("time_utc", keep="last")
    # A Series indexed by time makes "value at time t" lookups easy (see _value_near).
    return pd.Series(s["value"].to_numpy(float), index=pd.DatetimeIndex(s["time_utc"]))


def _value_near(series: pd.Series, t: pd.Timestamp, tol_hours: float) -> float:
    if series.empty:
        return np.nan
    # Position of the reading closest to t, or -1 if none is within the tolerance. A missing hour then
    # gives NaN instead of borrowing a reading from far away.
    i = series.index.get_indexer([t], method="nearest", tolerance=pd.Timedelta(hours=tol_hours))[0]
    return float(series.iloc[i]) if i >= 0 else np.nan


def obs_sites(cfg: dict, point: str) -> list[tuple[str, str, str]]:
    """(site, element, kind) for each obs site a point is verified against.

    kind "snotel": hourly gauge precip (PREC; its snow pillow is the fallback, see observed_water);
    "synoptic": hourly gauge precip (PREC);
    "ghcn": NOAA daily 24 h precip total (PRCP), one value per observer day.
    """
    p = cfg["points"][point]
    sites = p.get("obs") or ([p["snotel"]] if p.get("snotel") else [])
    out = []
    for site in sites:
        if site in (cfg.get("ghcn") or {}):
            out.append((site, "PRCP", "ghcn"))
        elif site in (cfg.get("synoptic") or {}):
            out.append((site, "PREC", "synoptic"))
        else:
            out.append((site, "PREC", "snotel"))
    return out


def window_end_hour(cfg: dict, site: str | None, kind: str) -> int:
    """UTC hour the 24 h windows end for this site: the shared verify setting, or a NOAA observer's own time."""
    if kind == "ghcn":
        local = cfg["ghcn"][site].get("obs_hour_local", 18)
        return int((local - cfg["local_settings"]["standard_offset_hours"]) % 24)  # 18 MST -> 01 UTC
    return cfg["verify"]["window_end_utc_hour"]


def _window_change(series: pd.Series, s: pd.Timestamp, e: pd.Timestamp, element: str, tol_hours: float,
                   gcfg: dict) -> float:
    """Observed liquid over (s, e]. A precip gauge is noisy and resets when serviced, so its hourly precip comes from
    cottonwoods.gauge (with a day of context before the window, so the gauge's level is known at the start)."""
    start, end = _value_near(series, s, tol_hours), _value_near(series, e, tol_hours)
    if np.isnan(start) or np.isnan(end):
        return np.nan
    if element != "PREC":
        return end - start
    near = series[(series.index >= s - pd.Timedelta(hours=24)) & (series.index <= e + pd.Timedelta(hours=2))]
    inc = gauge_increments(near, gcfg)
    return float(inc[(inc.index > s) & (inc.index <= e)].sum())


def observed_water(gauge: pd.Series, pillow: pd.Series | None, s: pd.Timestamp, e: pd.Timestamp, element: str,
                   cfg: dict) -> dict:
    """Observed liquid over (s, e]: the gauge's, else the pillow's rise when the pillow has snowpack (a bare pillow
    reads 0 whatever falls). Both are kept, with the undercatch flag: pillow rose >= min_rise_in and more than
    undercatch_ratio x the gauge. pillow: the site's WTEQ series, or None for a site without one."""
    v = cfg["verify"]
    pc, tol = v["pillow_check"], v["swe_tolerance_hours"]
    g = _window_change(gauge, s, e, element, tol, cfg["gauge"])
    snowpack = _value_near(pillow, s, tol) if pillow is not None else np.nan
    p = _window_change(pillow, s, e, "WTEQ", tol, cfg["gauge"]) if snowpack >= pc["min_snowpack_in"] else np.nan
    use_gauge = not np.isnan(g)
    flag = use_gauge and not np.isnan(p) and p >= pc["min_rise_in"] and p > pc["undercatch_ratio"] * g
    return {"obs_element": element if use_gauge or np.isnan(p) else "WTEQ", "swe_obs_in": g if use_gauge else p,
            "gauge_obs_in": g, "pillow_obs_in": p, "snowpack_in": snowpack, "undercatch_flag": bool(flag)}


def noaa_water(prcp: float) -> dict:
    """observed_water's columns for a NOAA coop day: its precip total is a gauge reading; no pillow."""
    return {"obs_element": "PRCP", "swe_obs_in": prcp, "gauge_obs_in": prcp, "pillow_obs_in": np.nan,
            "snowpack_in": np.nan, "undercatch_flag": False}


def _windows(g: pd.DataFrame, end_hour: int, win: pd.Timedelta):
    """Yield (start, end, steps) for each full 24 h window ending at end_hour UTC that one run covers."""
    # Find the first window end whose whole 24 h falls after the run starts. normalize() = midnight of that
    # day. Then step forward one day at a time until the run's forecast ends.
    e = g["valid_start"].min().normalize() + pd.Timedelta(hours=end_hour)
    while e - win < g["valid_start"].min():
        e += pd.Timedelta(days=1)
    while e <= g["valid_end"].max():
        s = e - win
        sub = g[(g["valid_end"] > s) & (g["valid_end"] <= e)]
        # Keep the window only if model steps cover exactly the full 24 h, none start before it,
        # and no precip is missing.
        covered = (sub["valid_end"] - sub["valid_start"]).sum()
        if len(sub) > 0 and covered == win and sub["valid_start"].min() >= s and sub["qpf_mm"].notna().all():
            yield s, e, sub
        e += pd.Timedelta(days=1)


def build_pairs(fc: pd.DataFrame, obs: pd.DataFrame, cfg: dict, manual: pd.DataFrame | None = None) -> pd.DataFrame:
    v = cfg["verify"]
    win = pd.Timedelta(hours=v["window_hours"])
    tz = cfg["local_settings"]["standard_offset_hours"]  # hand-entered reports go by Utah local (MST) date
    has_snow = "snow_in" in fc.columns
    if "member" in fc.columns:
        fc = fc[fc["member"].isna()]  # ensembles are scored separately (member fractions), not window by window here
    obs = drop_unreliable_water(obs, cfg)  # years whose water readings are known to be bad -> observed NaN
    # Each site's time series is pulled out of the big obs table once and reused for every run (much faster).
    series_cache: dict[tuple[str, str], pd.Series] = {}

    def series(site, element):
        if (site, element) not in series_cache:
            series_cache[(site, element)] = _series(obs, site, element) if site and not obs.empty else pd.Series(dtype=float)
        return series_cache[(site, element)]

    rows = []
    # groupby splits the forecast table into one piece per (model, run, point); g is that piece.
    for (model, init, point), g in fc.groupby(["model", "init", "point"]):
        if point not in cfg["points"]:
            continue  # GFS's extra 2x2 grid cells: kept in the archive for later best-fit work, not verified here
        g = g.sort_values("valid_end")
        # A point with no obs site still gets rows (observed columns NaN), so the forecast side is kept.
        for site, element, kind in obs_sites(cfg, point) or [(None, "PREC", "snotel")]:
            # Windows are built per site: NOAA sites end at their observer's hour, others at the shared hour.
            for s, e, sub in _windows(g, window_end_hour(cfg, site, kind), win):
                if kind == "ghcn":
                    # A NOAA daily value is already the 24 h total, stamped at the window end; it must match exactly.
                    seen = noaa_water(_value_near(series(site, "PRCP"), e, 0))
                    new_snow = _value_near(series(site, "SNOW"), e, 0)
                else:
                    seen = observed_water(series(site, element), series(site, "WTEQ") if kind == "snotel" else None,
                                          s, e, element, cfg)
                    # Hand-entered snow reports go by local date; convert the window end from UTC to Utah time.
                    new_snow = np.nan
                    if manual is not None and not manual.empty:
                        local_date = (e + pd.Timedelta(hours=tz)).date()
                        m = manual[(manual["site"] == point) & (manual["date"] == local_date)]
                        if not m.empty:
                            new_snow = float(m["new_snow_in"].iloc[-1])
                lead = (e - init) / pd.Timedelta(hours=1)
                rows.append(
                    {
                        "model": model,
                        "init": init,
                        "point": point,
                        "site": site,
                        "window_start": s,
                        "window_end": e,
                        "lead_hr": lead,
                        "lead_bin": int(lead // 24) * 24,  # 36 h -> 24, i.e. the "24-48 h" group
                        "qpf_in": sub["qpf_mm"].sum() / MM_PER_IN,
                        "snow_in_model": sub["snow_in"].sum() if has_snow else np.nan,
                        **seen,
                        "new_snow_obs_in": new_snow,
                    }
                )
    return pd.DataFrame(rows)


def _ratio(num: float, den: float) -> float:
    # Division that returns NaN instead of crashing (or giving a misleading number) when the bottom is 0.
    return num / den if den and den > 0 else np.nan


def summarize(pairs: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Per model, obs site and lead bin: bias, error, and event detection (measurable-precip hits/misses)."""
    if pairs.empty:
        return pd.DataFrame()
    thr = cfg["verify"]["event_swe_in"]
    d = pairs.dropna(subset=["swe_obs_in"]).copy()
    d["swe_obs_in"] = d["swe_obs_in"].clip(lower=0)  # negative SWE change = settling/melt/noise, not negative precip
    out = []
    for (model, site, lead), g in d.groupby(["model", "site", "lead_bin"]):
        # True/False per window: did the obs / the model reach "measurable"? Then count the 4 outcomes:
        # hit (both yes), miss (obs yes, model no), false alarm (model yes, obs no). .sum() counts the Trues.
        obs_ev, mod_ev = g["swe_obs_in"] >= thr, g["qpf_in"] >= thr
        hits, misses, fa = int((obs_ev & mod_ev).sum()), int((obs_ev & ~mod_ev).sum()), int((~obs_ev & mod_ev).sum())
        snow = g.dropna(subset=["new_snow_obs_in", "snow_in_model"])
        out.append(
            {
                "model": model,
                "site": site,
                "lead_bin": lead,
                "lead_hr": f"{lead}-{lead + 24}",
                "n_windows": len(g),
                "mean_obs_swe_in": round(g["swe_obs_in"].mean(), 3),
                "mean_model_qpf_in": round(g["qpf_in"].mean(), 3),
                # bias ratio = total forecast / total observed: 1.0 is perfect, 1.3 = 30% too wet
                "qpf_bias_ratio": round(_ratio(g["qpf_in"].sum(), g["swe_obs_in"].sum()), 2),
                # MAE = mean absolute error: average size of the miss in inches, ignoring direction
                "qpf_mae_in": round((g["qpf_in"] - g["swe_obs_in"]).abs().mean(), 3),
                "pod": round(_ratio(hits, hits + misses), 2),
                "far": round(_ratio(fa, hits + fa), 2),
                "n_pillow_fallback": int((g["obs_element"] == "WTEQ").sum()),   # gauge missing, pillow used
                "n_undercatch_flag": int(g["undercatch_flag"].sum()),
                "n_snow_obs": len(snow),
                "snow_bias_ratio": round(_ratio(snow["snow_in_model"].sum(), snow["new_snow_obs_in"].sum()), 2)
                if len(snow)
                else np.nan,
            }
        )
    if not out:
        return pd.DataFrame()
    return pd.DataFrame(out).sort_values(["model", "site", "lead_bin"]).reset_index(drop=True)


def gauge_pillow_days(obs: pd.DataFrame, cfg: dict, snd: pd.DataFrame | None = None) -> pd.DataFrame:
    """Every SNOTEL site's gauge vs pillow, one row per 24 h window (the verification windows) its records cover, from
    observations alone (no model runs needed, so the whole archive counts): observed_water's columns plus the day's
    balloon flow and band (snd: sounding summaries indexed by time_utc; '?' without them). The undercatch evidence."""
    v = cfg["verify"]
    win = pd.Timedelta(hours=v["window_hours"])
    obs = drop_unreliable_water(obs, cfg)
    rows = []
    for site in cfg["snotel"]:
        gauge, pillow = _series(obs, site, "PREC"), _series(obs, site, "WTEQ")
        if gauge.empty or pillow.empty:
            continue
        e = gauge.index.min().normalize() + pd.Timedelta(hours=v["window_end_utc_hour"]) + win
        while e <= gauge.index.max():
            s = e - win
            r = regimes.balloon_regime(snd, [s, s + pd.Timedelta(hours=12)], cfg["regimes"]) if snd is not None else None
            rows.append({"site": site, "window_start": s, "window_end": e,
                         **observed_water(gauge, pillow, s, e, "PREC", cfg),
                         "flow": r["flow"] if r else "?", "band": r["band"] if r else "?"})
            e += pd.Timedelta(days=1)
    return pd.DataFrame(rows)


def undercatch_summary(days: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Per site and flow ('all' = every flow): on snowpack days where the pillow rose at least min_rise_in, the
    gauge's and the pillow's water and their ratio (pillow / gauge > 1 = the gauge caught less than landed), and how
    many windows were flagged. Evidence for a later undercatch correction; nothing is corrected here."""
    pc = cfg["verify"]["pillow_check"]
    d = days.dropna(subset=["gauge_obs_in", "pillow_obs_in"]) if not days.empty else days
    d = d[d["pillow_obs_in"] >= pc["min_rise_in"]] if not d.empty else d
    out = []
    for site, g in d.groupby("site"):
        for flow, f in [("all", g), *g.groupby("flow")]:
            out.append({"site": site, "flow": flow, "n_days": len(f), "gauge_in": round(f["gauge_obs_in"].sum(), 2),
                        "pillow_in": round(f["pillow_obs_in"].sum(), 2),
                        "pillow_to_gauge": round(_ratio(f["pillow_obs_in"].sum(), f["gauge_obs_in"].sum()), 2),
                        "n_flagged": int(f["undercatch_flag"].sum())})
    return pd.DataFrame(out)


def merge_gauge_pillow_days(new: pd.DataFrame, path) -> pd.DataFrame:
    """The stored day table (data/processed/gauge_pillow_days.csv) with `new` replacing its rows for the same site
    and window (readings get revised); saved and returned. A pass recomputes only its recent days this way."""
    if path.exists():
        old = pd.read_csv(path, parse_dates=["window_start", "window_end"])
        keep = ~old.set_index(["site", "window_end"]).index.isin(new.set_index(["site", "window_end"]).index)
        new = pd.concat([old[keep], new], ignore_index=True) if not new.empty else old
    new = new.sort_values(["site", "window_end"]).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    new.to_csv(path, index=False)
    return new
