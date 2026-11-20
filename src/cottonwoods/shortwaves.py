"""500 mb shortwave troughs and vorticity advection (owner, 2026-11-19), from the pattern archive (patterns.py:
z500_m + vort500_s per run, every 6 h; "native" = each model's own grid around Utah, "1deg" = Pacific to Plains).
Thresholds in config shortwaves. UNCALIBRATED first version: measured against the backfill storms before use.

  shortwaves    band-passed vorticity (smoothed at detail_km minus smoothed at background_km) isolates
                shortwave-scale lows;
                local maxima of that anomaly >= min_anomaly_s, at least peak_spacing_km apart, are the troughs
  tracks        maxima linked map to map (nearest within max_speed_kmh x hours apart, strongest first), so each
                trough has a path, a speed and a strength over time
  passage       when a track crosses the point's longitude (linear between maps): time, latitude (+ = north of
                the point), strength; only within passage_lat_deg of the point
  PVA           vorticity advection by the geostrophic 500 mb wind (from the height gradient: the archive keeps
                250 mb winds only), absolute vorticity, -V.grad(zeta + f), mean over a disk of pva_radius_km around
                the point; > 0 = positive vorticity advection (large-scale rising air). Units 1e-9 s^-2 when shown.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

R_EARTH = 6.371e6
OMEGA = 7.2921e-5
G = 9.80665


def grids(df: pd.DataFrame, field: str) -> tuple[np.ndarray, np.ndarray, dict]:
    """(lats ascending, lons ascending, {time: 2-D array [lat, lon]}) of one field from pattern rows that carry a
    `time` column (valid time)."""
    lats, lons = np.sort(df["lat"].unique()), np.sort(df["lon"].unique())
    out = {}
    for t, g in df.groupby("time"):
        out[t] = (g.pivot_table(index="lat", columns="lon", values=field).reindex(index=lats, columns=lons)
                  .to_numpy(float))
    return lats, lons, out


def _spacing_m(lats, lons):
    """Grid spacing (m): dy scalar, dx per latitude row."""
    dlat, dlon = np.deg2rad(np.diff(lats).mean()), np.deg2rad(np.diff(lons).mean())
    return R_EARTH * dlat, R_EARTH * dlon * np.cos(np.deg2rad(lats))


def anomaly(vort: np.ndarray, lats, lons, background_km: float, detail_km: float = 0.0) -> np.ndarray:
    """Band-pass vorticity: smoothed at detail_km (removes grid-scale streaks: terrain, banding) minus smoothed at
    background_km (removes the long waves). What is left is shortwave-scale (Gaussian sigmas)."""
    from scipy.ndimage import gaussian_filter
    dy, dx = _spacing_m(lats, lons)

    def smooth(v, km):
        if km <= 0:
            return v
        return gaussian_filter(v, (km * 1000 / dy, km * 1000 / float(np.mean(dx))), mode="nearest")
    v = np.where(np.isfinite(vort), vort, np.nanmean(vort))
    return smooth(v, detail_km) - smooth(v, background_km)


def maxima(anom: np.ndarray, lats, lons, cfg: dict) -> pd.DataFrame:
    """Local maxima of the anomaly >= min_anomaly_s, at least peak_spacing_km apart (edge rows / columns left out:
    a maximum there may be a feature outside the box). Columns lat, lon, anom."""
    from scipy.ndimage import maximum_filter
    dy, dx = _spacing_m(lats, lons)
    size = (max(3, int(2 * cfg["peak_spacing_km"] * 1000 / dy) | 1),
            max(3, int(2 * cfg["peak_spacing_km"] * 1000 / float(np.mean(dx))) | 1))
    a = np.where(np.isfinite(anom), anom, -np.inf)
    peak = (a == maximum_filter(a, size=size, mode="nearest")) & (a >= cfg["min_anomaly_s"])
    peak[[0, -1], :] = peak[:, [0, -1]] = False
    i, j = np.nonzero(peak)
    return pd.DataFrame({"lat": lats[i], "lon": lons[j], "anom": a[i, j]})


def km_between(lat1, lon1, lat2, lon2):
    p1, p2 = np.deg2rad(lat1), np.deg2rad(lat2)
    h = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.deg2rad(lon2 - lon1) / 2) ** 2
    return 2 * R_EARTH / 1000 * np.arcsin(np.sqrt(np.clip(h, 0, 1)))


def _predict(pts: list[dict], t, cfg: dict) -> tuple[float, float]:
    """Where a track should be at time t: its last position moved by its last velocity (two or more points), else
    by first_guess_kmh eastward."""
    e = pts[-1]
    hrs = (t - e["time"]) / pd.Timedelta(hours=1)
    if len(pts) >= 2:
        p = pts[-2]
        h0 = (e["time"] - p["time"]) / pd.Timedelta(hours=1)
        return e["lat"] + (e["lat"] - p["lat"]) / h0 * hrs, e["lon"] + (e["lon"] - p["lon"]) / h0 * hrs
    dlon = cfg["first_guess_kmh"] * hrs / (111.2 * np.cos(np.deg2rad(e["lat"])))
    return e["lat"], e["lon"] + dlon


def track(found: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Link maxima (time, lat, lon, anom) map to map into tracks. Each open track (last seen within max_gap_hours)
    predicts where it should be (_predict); maxima and tracks are paired all at once to minimise the total distance
    to the predictions (optimal assignment), a pair allowed only within match_radius_km of the prediction, at most
    max_speed_kmh from the track's last position and moving west at most allow_west_kmh (troughs here move east).
    Unpaired maxima start tracks. Adds `track`; tracks seen on fewer than min_maps maps are dropped."""
    from scipy.optimize import linear_sum_assignment
    rows, open_, nxt = [], {}, 0                       # open_: track id -> its points so far
    big = 1e9
    for t, g in found.sort_values("time").groupby("time"):
        g = g.reset_index(drop=True)
        ids = [k for k, pts in open_.items()
               if 0 < (t - pts[-1]["time"]) / pd.Timedelta(hours=1) <= cfg["max_gap_hours"]]
        cost = np.full((len(ids), len(g)), big)
        for a, tid in enumerate(ids):
            pts = open_[tid]
            e = pts[-1]
            hrs = (t - e["time"]) / pd.Timedelta(hours=1)
            plat, plon = _predict(pts, t, cfg)
            for b, m in g.iterrows():
                d_pred = km_between(plat, plon, m["lat"], m["lon"])
                d_last = km_between(e["lat"], e["lon"], m["lat"], m["lon"])
                west = km_between(e["lat"], e["lon"], e["lat"], m["lon"]) if m["lon"] < e["lon"] else 0.0
                if (d_pred <= cfg["match_radius_km"] and d_last <= cfg["max_speed_kmh"] * hrs
                        and west <= cfg["allow_west_kmh"] * hrs):
                    cost[a, b] = d_pred
        pair = {}
        if len(ids) and len(g):
            for a, b in zip(*linear_sum_assignment(cost)):
                if cost[a, b] < big:
                    pair[b] = ids[a]
        for b, m in g.iterrows():
            tid = pair.get(b)
            if tid is None:
                tid, nxt = nxt, nxt + 1
                open_[tid] = []
            row = {"track": tid, "time": t, "lat": m["lat"], "lon": m["lon"], "anom": m["anom"]}
            open_[tid].append(row)
            rows.append(row)
    out = pd.DataFrame(rows, columns=["track", "time", "lat", "lon", "anom"])
    if out.empty:
        return out
    n = out.groupby("track")["time"].transform("size")
    return out[n >= cfg["min_maps"]].reset_index(drop=True)


def passages(tracks: pd.DataFrame, lat0: float, lon0: float, cfg: dict) -> pd.DataFrame:
    """Each track's crossing of lon0 (linear between the two maps either side): time, lat, dlat (+ = north of the
    point), strength (anom), speed (km/h over that leg). Only crossings within passage_lat_deg of lat0."""
    rows = []
    for tid, g in tracks.sort_values("time").groupby("track"):
        g = g.reset_index(drop=True)
        for k in range(len(g) - 1):
            a, b = g.iloc[k], g.iloc[k + 1]
            if a["lon"] < lon0 <= b["lon"]:
                f = (lon0 - a["lon"]) / (b["lon"] - a["lon"])
                lat = a["lat"] + f * (b["lat"] - a["lat"])
                if abs(lat - lat0) > cfg["passage_lat_deg"]:
                    continue
                hrs = (b["time"] - a["time"]) / pd.Timedelta(hours=1)
                rows.append({"track": tid, "time": a["time"] + f * (b["time"] - a["time"]), "lat": lat,
                             "dlat": lat - lat0, "anom": a["anom"] + f * (b["anom"] - a["anom"]),
                             "speed_kmh": km_between(a["lat"], a["lon"], b["lat"], b["lon"]) / hrs})
    return pd.DataFrame(rows, columns=["track", "time", "lat", "dlat", "anom", "speed_kmh"])


def vort_advection(z: np.ndarray, vort: np.ndarray, lats, lons) -> np.ndarray:
    """-Vg . grad(zeta + f) (s^-2): geostrophic wind from the 500 mb height (ug = -g/f dz/dy, vg = g/f dz/dx),
    absolute vorticity. > 0 = positive vorticity advection."""
    dy, dx = _spacing_m(lats, lons)
    f = 2 * OMEGA * np.sin(np.deg2rad(lats))[:, None]
    dzdy, dzdx = np.gradient(z, dy, axis=0), np.gradient(z, axis=1) / dx[:, None]
    ug, vg = -G / f * dzdy, G / f * dzdx
    eta = vort + f
    dedy, dedx = np.gradient(eta, dy, axis=0), np.gradient(eta, axis=1) / dx[:, None]
    return -(ug * dedx + vg * dedy)


def disk_mean(field: np.ndarray, lats, lons, lat0: float, lon0: float, radius_km: float) -> float:
    la, lo = np.meshgrid(lats, lons, indexing="ij")
    inside = km_between(lat0, lon0, la, lo) <= radius_km
    v = field[inside]
    return float(np.nanmean(v)) if np.isfinite(v).any() else np.nan


def analyse(df: pd.DataFrame, lat0: float, lon0: float, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(series, tracks, passages) from pattern rows with a `time` column (one grid): series = per map time the PVA
    at the point (pva, s^-2) and the strongest shortwave anomaly within pva_radius_km (anom_near)."""
    lats, lons, zg = grids(df, "z500_m")
    _, _, vg = grids(df, "vort500_s")
    found, series = [], []
    for t in sorted(vg):
        an = anomaly(vg[t], lats, lons, cfg["background_km"], cfg["detail_km"])
        m = maxima(an, lats, lons, cfg)
        found.append(m.assign(time=t))
        adv = vort_advection(zg[t], vg[t], lats, lons)
        series.append({"time": t, "pva": disk_mean(adv, lats, lons, lat0, lon0, cfg["pva_radius_km"]),
                       "anom_near": float(np.nanmax(np.where(
                           km_between(lat0, lon0, *np.meshgrid(lats, lons, indexing="ij")) <= cfg["pva_radius_km"],
                           an, np.nan)))})
    tr = track(pd.concat(found, ignore_index=True), cfg) if found else pd.DataFrame()
    return pd.DataFrame(series), tr, passages(tr, lat0, lon0, cfg) if not tr.empty else pd.DataFrame()


def stitch_analyses(runs: list[pd.DataFrame]) -> pd.DataFrame:
    """A near-observed map series from consecutive runs: each run's EARLIEST map (the 0 h analysis in the backfill's
    maps, 6 h in the live archive, which has no 0 h), valid time init + lead. Runs: pattern rows of one grid, each
    with its `init`. Where two runs give the same valid time, the shorter lead wins."""
    parts = [r[r["lead_hr"] == r["lead_hr"].min()].assign(time=lambda d: d["init"] + pd.to_timedelta(d["lead_hr"], unit="h"))
             for r in runs if not r.empty]
    if parts:
        return pd.concat(parts, ignore_index=True).sort_values("lead_hr").drop_duplicates(["time", "lat", "lon"])
    return pd.DataFrame()
