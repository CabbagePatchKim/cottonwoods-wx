"""Time-height features from model point soundings (owner, 2026-11-19; models/bufkit.py supplies the profiles):
per forecast hour, the things the owner reads off a time-height section, thresholds in config timeheight.
UNCALIBRATED first version: measure against verified storms before trusting any threshold.

  freezing level / wet-bulb zero   highest 0 C crossing (MSL); a profile below 0 C all the way down is stored as the
                                   model's ground with *_below_ground True (same convention as profile.py)
  moist layer       the deepest run of levels with RH >= rh_moist (over ice below 0 C, over water above) whose base
                    is within moist_base_max_agl_m of the model's ground (cloud that can snow on the point):
                    base / top (MSL), depth, temperature at its top (cold tops make ice; warm tops starve snow growth)
  growth zone       dgz_sat_m: depth of saturated air between dgz_c (-18 .. -12 C), where dendrites grow fastest;
                    dgz_omega: strongest upward motion there (microbar/s, negative = rising); at a mountain point
                    this is mostly the local terrain's draft (HRRR at Alta), so read it with care
  stability         thetae_drop_k: how far theta-e falls with height inside the moist layer (lowest theta-e above
                    its bottom km vs the bottom km's highest; > 0 = potentially unstable: lifting the layer makes
                    cells); thetae_sfc_top_k: theta-e at the model's ground minus at stab_top_hpa (500 mb), > 0 =
                    unstable; lapse_sfc_top (C/km) over the same layer; cape (the file's). From the ground, not
                    700 mb: at Alta 700 mb is below the model's ground in deep lows (2/17/2026 HRRR)
  convective        convective True when the moist layer is at least conv_min_depth_m deep AND thetae_drop_k is at
                    least conv_min_thetae_drop_k (the owner's "unstable theta-e + deep moisture = high snow rates")
  overrunning       cloud_base_m / subcloud_m / subcloud_rh / subcloud_dry_m (_overrunning): a cloud aloft over dry
                    air sublimates light snow before it reaches the ground (onset delay, owner 2026-11-19)
  spillover         ucross_ms / n2 / froude / gen_m / gen_c / drift_km (_spillover): crest-made snow carried
                    east onto the Wasatch back (Park City) instead of falling in the Cottonwoods
  front             the run's frontal passage by the live rule (modes.find_front: largest 6 h cooling >=
                    regimes.front_drop_c with a veer >= front_veer_deg) at front_hpa (650 mb, above Alta's
                    terrain in every model, where the live rule's 700 mb can be underground), front True there
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .modes import find_front
from .profile import _crossing, e_sat


def e_ice(t_c):
    """Saturation vapor pressure over ice (hPa), Magnus form (Alduchov & Eskridge)."""
    t = np.asarray(t_c, float)
    return 6.112 * np.exp(22.46 * t / (t + 272.62))


def rh_ice_water(t_c, td_c):
    """RH (%): over ice where T < 0 C, over water otherwise (saturation that matters for snow growth)."""
    t, td = np.asarray(t_c, float), np.asarray(td_c, float)
    return 100 * e_sat(td) / np.where(t < 0, e_ice(t), e_sat(t))


def _runs(mask):
    """[(i, j)] index spans (inclusive) of consecutive True values."""
    out, start = [], None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        if not m and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(mask) - 1))
    return out


def _at(p, v, level):
    """v interpolated to pressure `level` (p descending); NaN outside the profile."""
    if not (p.min() <= level <= p.max()):
        return np.nan
    return float(np.interp(level, p[::-1], v[::-1]))


def hour_features(lv: pd.DataFrame, tc: dict) -> dict:
    """Features of one hour's profile (rows of bufkit.parse levels for one time)."""
    lv = lv[lv["pres"] >= tc["top_hpa"]].sort_values("pres", ascending=False)
    p, t, tw, td = (lv[c].to_numpy(float) for c in ("pres", "tmpc", "tmwc", "dwpc"))
    z, te, om = lv["hght"].to_numpy(float), lv["thte"].to_numpy(float), lv["omeg"].to_numpy(float)
    ground = float(z[0])
    out = {"ground_m": ground}
    for name, v in (("fzl", t), ("wbz", tw)):
        lvl = _crossing(z, v, 0.0)
        below = bool(np.isnan(lvl) and v[0] <= 0)
        out[f"{name}_m"], out[f"{name}_below_ground"] = (ground if below else lvl), below
    rh = rh_ice_water(t, td)
    moist = rh >= tc["rh_moist"]
    best = None
    for i, j in _runs(moist):
        if z[i] - ground <= tc["moist_base_max_agl_m"] and (best is None or z[j] - z[i] > z[best[1]] - z[best[0]]):
            best = (i, j)
    if best is None:
        out.update(moist_base_m=np.nan, moist_top_m=np.nan, moist_depth_m=0.0, moist_top_c=np.nan, thetae_drop_k=np.nan)
    else:
        i, j = best
        zs, tes = z[i:j + 1], te[i:j + 1]
        low = zs <= zs[0] + 1000                    # the layer's bottom km
        out.update(moist_base_m=float(z[i]), moist_top_m=float(z[j]), moist_depth_m=float(z[j] - z[i]),
                   moist_top_c=float(t[j]),
                   thetae_drop_k=float(tes[low].max() - tes[~low].min()) if (~low).any() else 0.0)
    lo, hi = tc["dgz_c"]
    dgz = (t >= lo) & (t <= hi)
    dz = np.diff(z, append=z[-1])                   # each level's share of the column (to the next level up)
    out["dgz_sat_m"] = float(dz[dgz & moist].sum())
    out["dgz_omega"] = float(om[dgz].min()) if dgz.any() else np.nan
    # stability from the model's ground (not 700 mb: at Alta 700 mb is underground in deep lows) to stab_top_hpa
    top = tc["stab_top_hpa"]
    t5, z5 = _at(p, t, top), _at(p, z, top)
    out["lapse_sfc_top"] = (t[0] - t5) / ((z5 - ground) / 1000) if np.isfinite(z5) else np.nan
    out["thetae_sfc_top_k"] = te[0] - _at(p, te, top)
    # the front's level (front_hpa: above Alta's terrain in every model)
    fl = tc["front_hpa"]
    out["t_front_c"] = _at(p, t, fl)
    # wind there from interpolated components (interpolating directions breaks across north: 350/10 -> 180)
    rad, kt = np.deg2rad(lv["drct"].to_numpy(float)), lv["sknt"].to_numpy(float)
    u, v = _at(p, -kt * np.sin(rad), fl), _at(p, -kt * np.cos(rad), fl)
    out["dir_front"] = float(np.rad2deg(np.arctan2(-u, -v)) % 360) if np.isfinite(u) else np.nan
    out["kt_front"] = float(np.hypot(u, v)) if np.isfinite(u) else np.nan
    out["convective"] = bool(out["moist_depth_m"] >= tc["conv_min_depth_m"]
                             and np.nan_to_num(out["thetae_drop_k"]) >= tc["conv_min_thetae_drop_k"])
    out.update(_overrunning(z, rh, moist, dz, ground, tc))
    out.update(_spillover(p, z, t, te, moist, -kt * np.sin(rad) * KT_TO_MS, -kt * np.cos(rad) * KT_TO_MS,
                          ground, tc))
    return out


KT_TO_MS = 0.514444
G = 9.81


def _overrunning(z, rh, moist, dz, ground, tc) -> dict:
    """The precipitating cloud's base and the air under it: cloud_base_m = base of the lowest saturated layer at
    least cloud_min_depth_m deep (anywhere, unlike the moist layer); subcloud_m = its height above the model's
    ground (0 = cloud on the ground); subcloud_rh = depth-weighted RH (over ice) below it; subcloud_dry_m = that
    depth weighted by dryness (sum of (1 - RH) x thickness): ~how much falling snow the gap can sublimate."""
    for i, j in _runs(moist):
        if z[j] - z[i] >= tc["cloud_min_depth_m"]:
            below = slice(0, i)
            if i == 0:
                return {"cloud_base_m": float(z[0]), "subcloud_m": 0.0, "subcloud_rh": np.nan, "subcloud_dry_m": 0.0}
            w = dz[below]
            return {"cloud_base_m": float(z[i]), "subcloud_m": float(z[i] - ground),
                    "subcloud_rh": float((rh[below] * w).sum() / w.sum()) if w.sum() > 0 else np.nan,
                    "subcloud_dry_m": float((np.clip(1 - rh[below] / 100, 0, 1) * w).sum())}
    return {"cloud_base_m": np.nan, "subcloud_m": np.nan, "subcloud_rh": np.nan, "subcloud_dry_m": np.nan}


def _spillover(p, z, t, te, moist, u, v, ground, tc) -> dict:
    """Where crest-made snow lands (the Wasatch back vs the Cottonwoods):
      ucross_ms   wind across the barrier (toward its lee; barrier_normal_deg = the direction it comes FROM) at
                  crest_m (or the model's ground if that is higher)
      n2          bulk stability over stab_layer_m above the ground: theta-e where the layer is mostly saturated
                  (moist), theta otherwise (1/s^2; <= 0 = unstable)
      froude      ucross / (N x barrier_height_m): low = blocked (lift and snow shift upstream, west), high = flow
                  goes over (snow carried east); 99 when unstable, 0 with no cross-barrier wind
      gen_m/gen_c where the snow is made: mean height of the saturated growth zone (dgz_c) within gen_max_agl_m,
                  else of all saturated air there (not the strongest lift: see the note in the code)
      drift_km    sideways travel while falling from gen_m to the ground: mean cross-barrier wind below gen_m x
                  fall time at fall_speed_ms (cold = dendrites at or below fall_cold_c, warm = rimed)"""
    zc = max(tc["crest_m"], ground)
    nrm = np.deg2rad(tc["barrier_normal_deg"] + 180)            # the direction the cross-barrier flow goes TO
    along = u * np.sin(nrm) + v * np.cos(nrm)
    ucross = float(np.interp(zc, z, along)) if z[-1] >= zc else np.nan
    top = ground + tc["stab_layer_m"]
    theta = (t + 273.15) * (1000 / p) ** 0.2857
    layer = z <= top
    use = te if moist[layer].mean() >= 0.5 else theta
    zt = min(top, z[-1])
    a, b = float(np.interp(ground, z, use)), float(np.interp(zt, z, use))
    n2 = G / ((a + b) / 2) * (b - a) / (zt - ground) if zt > ground else np.nan
    if not np.isfinite(ucross) or not np.isfinite(n2):
        froude = np.nan
    elif ucross <= 0:
        froude = 0.0
    else:
        froude = 99.0 if n2 <= 0 else min(99.0, ucross / (np.sqrt(n2) * tc["barrier_height_m"]))
    # where the snow is made: the saturated growth zone if any (most snow mass grows there), else the saturated air,
    # within gen_max_agl_m. NOT the strongest lift: at a mountain point omega is mostly the local terrain's up/down
    # draft (HRRR at Alta: mean column omega positive = sinking in wet AND dry hours, 25 runs; GFS: negative in
    # heavy precip as expected; checked 2026-11-19)
    lo, hi = tc["dgz_c"]
    near = moist & (z <= ground + tc["gen_max_agl_m"])
    zone = near & (t >= lo) & (t <= hi) if (near & (t >= lo) & (t <= hi)).any() else near
    dz = np.diff(z, append=z[-1]) + 1e-6
    if zone.any():
        gen = float((z[zone] * dz[zone]).sum() / dz[zone].sum())
        gen_c = float(np.interp(gen, z, t))
        vf = tc["fall_speed_ms"]["cold" if gen_c <= tc["fall_cold_c"] else "warm"]
        below = z <= gen
        mean_cross = float(np.mean(along[below])) if below.any() else 0.0
        drift = max(0.0, mean_cross) * (gen - ground) / vf / 1000
    else:
        gen = gen_c = drift = np.nan
    return {"ucross_ms": ucross, "n2": n2, "froude": froude, "gen_m": gen, "gen_c": gen_c, "drift_km": drift}


def features(levels: pd.DataFrame, hours: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """One row per forecast hour: hour_features + cape / p01m (model precip in the hour, mm) + front."""
    tc = cfg["timeheight"]
    rows = [{"time": t, **hour_features(g, tc)} for t, g in levels.groupby("time")]
    out = pd.DataFrame(rows)
    keep = [c for c in ("time", "cape", "pwat", "p01m") if c in hours.columns]
    out = out.merge(hours[keep], on="time", how="left")
    # one front per run (the largest qualifying cooling); a long run spanning two storms shows only the stronger
    # (modes.find_front reads the columns by their 700 mb names; here they hold the front_hpa level)
    front_in = pd.DataFrame({"valid_end": out["time"], "t_700_k": out["t_front_c"] + 273.15,
                             "flow_dir700": out["dir_front"]})
    when = find_front(front_in, cfg["regimes"])
    out["front"] = out["time"] == when if when is not None else False
    # a front at the run's last hour: the cooling may go on past the run, so its time is only "at or after"
    out.attrs["front_at_end"] = when is not None and when == out["time"].max()
    return out


def first_hours(texts: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(levels, hours) of each run's FIRST profile, stacked: hourly RAP runs -> a near-observed (analysis)
    time-height series. A run whose text doesn't parse is skipped."""
    from .models.bufkit import parse
    lv, hr = [], []
    for text in texts:
        levels, hours = parse(text)
        if levels.empty:
            continue
        t0 = levels["time"].min()
        lv.append(levels[levels["time"] == t0])
        hr.append(hours[hours["time"] == t0])
    if not lv:
        return pd.DataFrame(), pd.DataFrame()
    return (pd.concat(lv, ignore_index=True).drop_duplicates(["time", "pres"]),
            pd.concat(hr, ignore_index=True).drop_duplicates("time"))


def _circ_mean(deg, weights=None) -> float:
    d = np.deg2rad(np.asarray(deg, float))
    w = np.ones_like(d) if weights is None else np.asarray(weights, float)
    ok = np.isfinite(d) & np.isfinite(w)
    if not ok.any():
        return np.nan
    return float(np.rad2deg(np.arctan2((w[ok] * np.sin(d[ok])).sum(), (w[ok] * np.cos(d[ok])).sum())) % 360)


def block_features(feats: pd.DataFrame, a, b, prefix: str = "") -> dict:
    """Summary of the hourly features in the block (a, b] (an hour's profile at time t stands for the hour ending
    at t): share of convective hours, mean / max moist depth, strongest theta-e drop, mean ground-500 theta-e,
    saturated growth zone, strongest growth-zone lift, freezing / wet-bulb-zero level, front, CAPE, wind at the
    front level (direction as a circular mean)."""
    h = feats[(feats["time"] > a) & (feats["time"] <= b)]
    if h.empty:
        return {}
    extra = {k: float(h[c].mean()) for k, c in (("subcloud_mean", "subcloud_m"), ("subcloud_dry_mean", "subcloud_dry_m"),
                                                 ("ucross_mean", "ucross_ms"), ("drift_mean", "drift_km"),
                                                 ("gen_c_mean", "gen_c")) if c in h}
    if "froude" in h:
        extra["froude_median"] = float(h["froude"].median())
    out = {**extra, "hours": len(h), "conv_share": float(h["convective"].mean()),
           "moist_depth_mean": float(h["moist_depth_m"].mean()), "moist_depth_max": float(h["moist_depth_m"].max()),
           "moist_top_c_min": float(h["moist_top_c"].min()), "thetae_drop_max": float(h["thetae_drop_k"].max()),
           "thetae_sfc_top_mean": float(h["thetae_sfc_top_k"].mean()), "lapse_mean": float(h["lapse_sfc_top"].mean()),
           "dgz_sat_mean": float(h["dgz_sat_m"].mean()), "dgz_omega_min": float(h["dgz_omega"].min()),
           "fzl_mean": float(h["fzl_m"].mean()), "fzl_below_share": float(h["fzl_below_ground"].mean()),
           "wbz_mean": float(h["wbz_m"].mean()), "t_front_mean": float(h["t_front_c"].mean()),
           "front": bool(h["front"].any()), "cape_max": float(h["cape"].max()) if "cape" in h else np.nan,
           "dir_front": _circ_mean(h["dir_front"], h["kt_front"]), "kt_front": float(h["kt_front"].mean())}
    return {prefix + k: v for k, v in out.items()}


def blocks(start, end, hours: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """[(a, b)] consecutive `hours`-long blocks from start; the last one is clipped at end."""
    out, a = [], pd.Timestamp(start)
    while a < end:
        b = min(a + pd.Timedelta(hours=hours), pd.Timestamp(end))
        out.append((a, b))
        a = b
    return out


def onset(steps: pd.DataFrame, start, end, threshold_in: float):
    """First time the water since `start` reaches threshold_in (steps: valid_start / valid_end / qpf_mm), or None
    if it never does before `end`. Within a step, water is spread evenly (a 3 h step reaches it part-way)."""
    from .units import MM_PER_IN
    s = steps[(steps["valid_end"] > start) & (steps["valid_start"] < end)].sort_values("valid_start")
    total, need = 0.0, threshold_in * MM_PER_IN
    for r in s.itertuples():
        a = max(r.valid_start, start)
        length = (r.valid_end - r.valid_start) / pd.Timedelta(hours=1)
        share = ((r.valid_end - a) / pd.Timedelta(hours=1)) / length if length > 0 else 0
        w = np.nan_to_num(r.qpf_mm) * share
        if w > 0 and total + w >= need:
            return a + (r.valid_end - a) * ((need - total) / w)
        total += w
    return None


def onset_row(storm, cfg: dict, obs_steps: pd.DataFrame, analysis: pd.DataFrame, run: dict) -> dict:
    """Onset timing for one storm and run (owner 2026-11-19, overrunning): observed and forecast onset (first
    timeheight.onset_in of water from pad_hours before the storm), the error in hours (+ = model early), and the
    dry gap under the cloud in the onset_window_hours before each onset: an_* from the analyses before the
    observed onset (what was really there), fc_* from the run's own profile before its onset (what it forecast)."""
    tc = cfg["timeheight"]
    s0, e = storm.final_start - pd.Timedelta(hours=tc["pad_hours"]), storm.final_end
    obs_on, fc_on = onset(obs_steps, s0, e, tc["onset_in"]), onset(run["steps"], s0, e, tc["onset_in"])
    row = {"storm_id": storm.storm_id, "model": run["model"], "lead_h": run["lead"], "init": run["init"],
           "obs_onset": obs_on, "fc_onset": fc_on,
           "onset_err_h": (obs_on - fc_on) / pd.Timedelta(hours=1) if obs_on is not None and fc_on is not None else np.nan}
    w = pd.Timedelta(hours=tc["onset_window_hours"])
    for pre, feats, t in (("an_", analysis, obs_on), ("fc_", run["feats"], fc_on)):
        if feats is not None and t is not None and len(feats):
            row.update(block_features(feats, t - w, t, pre))
    return row


def pillow_gain(obs: pd.DataFrame, site: str, start, end) -> float:
    """Snow-pillow (WTEQ) water gained over [start, end] at `site` (inches): median of the 3 h after the end minus
    median of the 3 h before the start (hourly pillow readings flicker by +-0.1"). A cross-check on the gauge,
    which undercatches in wind (spillover cases are windy). NaN without readings."""
    p = obs[(obs["site"] == site) & (obs["element"] == "WTEQ")]
    t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    before = p[(p["time_utc"] > t0 - pd.Timedelta(hours=3)) & (p["time_utc"] <= t0)]["value"]
    after = p[(p["time_utc"] >= t1) & (p["time_utc"] < t1 + pd.Timedelta(hours=3))]["value"]
    if before.empty or after.empty:
        return np.nan
    return float(after.median() - before.median())


def storm_block_rows(storm, cfg: dict, obs_steps: pd.DataFrame, analysis: pd.DataFrame, runs: list[dict],
                     sides: dict[str, pd.DataFrame] | None = None) -> list[dict]:
    """The measurement table for one storm: a row per (block of timeheight.block_hours, run), with the observed
    gauge water in the block (obs_in), the analysis features (an_*), the run's water (fc_in, from its archived
    steps) and, when the run has a BUFKIT file, its own forecast features (fc_*). A block with no run still gets one
    row (model None), so the observed table counts every block once. runs: [{model, lead, init, steps, feats}].
    sides: {name: gauge steps} of other sites (spillover: back = Thaynes Canyon, front = Brighton) -> obs_<name>_in."""
    from .units import MM_PER_IN
    from .windows import window_total
    rows = []
    for a, b in blocks(storm.final_start, storm.final_end, cfg["timeheight"]["block_hours"]):
        side = {f"obs_{k}_in": (window_total(v, a, b) / MM_PER_IN if not v.empty else np.nan)
                for k, v in (sides or {}).items()}
        base = {"storm_id": storm.storm_id, "block_start": a, "block_end": b,
                "obs_in": window_total(obs_steps, a, b) / MM_PER_IN, **side, **block_features(analysis, a, b, "an_")}
        made = False
        for r in runs:
            st = r["steps"]
            if st.empty or st["valid_start"].min() > a or st["valid_end"].max() < b:
                continue                                         # the run doesn't cover the whole block
            fc = r["feats"]
            rows.append({**base, "model": r["model"], "lead_h": r["lead"], "init": r["init"],
                         "fc_in": window_total(st, a, b) / MM_PER_IN,
                         **(block_features(fc, a, b, "fc_") if fc is not None else {})})
            made = True
        if not made:
            rows.append({**base, "model": None})
    return rows


def grid(levels: pd.DataFrame, cols: list[str], z_m: np.ndarray) -> dict[str, np.ndarray]:
    """{col: array (hours x heights)} of each column interpolated to heights z_m (MSL); NaN below the model's
    ground or above its top. Adds "rh" (over ice below 0 C)."""
    lv = levels.assign(rh=rh_ice_water(levels["tmpc"], levels["dwpc"]))
    out = {c: [] for c in cols + ["rh"]}
    for _, g in lv.groupby("time"):
        g = g.sort_values("hght")
        for c in out:
            out[c].append(np.interp(z_m, g["hght"], g[c], left=np.nan, right=np.nan))
    return {c: np.array(v) for c, v in out.items()}


def plot(levels: pd.DataFrame, feats: pd.DataFrame, title: str, path, top_ft: float = 30000) -> None:
    """Time-height section (height in kft MSL, Mountain time): RH shading, isotherms (0 C bold, growth zone purple),
    theta-e (grey: lines folding back = theta-e falling with height = unstable), rising motion (blue), winds;
    strip above: model precip per hour, convective hours (orange), front (black line)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from .timefmt import local
    from .units import FT_PER_M
    z = np.arange(levels["hght"].min(), top_ft / FT_PER_M, 50.0)
    g = grid(levels, ["tmpc", "thte", "omeg", "drct", "sknt"], z)
    x, kft = np.arange(len(feats)), z * FT_PER_M / 1000
    fig, (ax0, ax) = plt.subplots(2, 1, figsize=(14, 8), sharex=True, gridspec_kw={"height_ratios": [1, 5]},
                                  layout="constrained")
    p = feats["p01m"].fillna(0) / 25.4
    ax0.bar(x, p, color=np.where(feats["convective"], "darkorange", "steelblue"))
    ax0.set_ylabel("in/h")
    ax0.set_title(title + "  (bars: model precip per hour; orange = convective hour)")
    cf = ax.contourf(x, kft, g["rh"].T, levels=[70, 80, 90, 100, 200], colors=["#e5f5e0", "#a1d99b", "#41ab5d", "#41ab5d"])
    c = ax.contour(x, kft, g["tmpc"].T, levels=np.arange(-48, 21, 4), colors="red", linewidths=0.7)
    ax.clabel(c, fmt="%d", fontsize=7)
    ax.contour(x, kft, g["tmpc"].T, levels=[0], colors="red", linewidths=2.2)
    ax.contour(x, kft, g["tmpc"].T, levels=[-18, -12], colors="purple", linewidths=1.6, linestyles="dashed")
    ct = ax.contour(x, kft, g["thte"].T, levels=np.arange(270, 340, 3), colors="dimgrey", linewidths=0.6)
    ax.clabel(ct, fmt="%d", fontsize=6)
    ax.contour(x, kft, g["omeg"].T, levels=[-20, -10, -5], colors="blue", linewidths=[1.6, 1.1, 0.7])
    zi = np.arange(0, len(z), max(1, len(z) // 12))
    xi = np.arange(0, len(x), 3)
    kt = g["sknt"][np.ix_(xi, zi)]
    rad = np.deg2rad(g["drct"][np.ix_(xi, zi)])
    ax.barbs(np.repeat(xi, len(zi)), np.tile(kft[zi], len(xi)), (-kt * np.sin(rad)).ravel(), (-kt * np.cos(rad)).ravel(),
             length=5, linewidth=0.5)
    for t in x[feats["front"].to_numpy(bool)]:
        for a in (ax0, ax):
            a.axvline(t, color="black", linewidth=2)
    ax.plot(x, feats["moist_top_m"] * FT_PER_M / 1000, color="darkgreen", linewidth=1, linestyle=":")
    ax.set_ylim(kft[0], top_ft / 1000)
    ax.set_ylabel("kft MSL")
    ax.set_xticks(x[::3])
    ax.set_xticklabels([local(t).strftime("%a %H") for t in feats["time"][::3]], rotation=60, fontsize=8)
    for a in (ax0, ax):
        a.set_xlim(-0.5, len(x) - 0.5)
    fig.colorbar(cf, ax=[ax0, ax], label="RH % (over ice below 0 C)", ticks=[70, 80, 90, 100], fraction=0.04)
    ax.text(0.01, 0.01, "red: T (C), bold 0 C | purple dashed: -12/-18 C growth zone | grey: theta-e (K) | "
            "blue: rising (-5/-10/-20 microbar/s) | green dots: moist-layer top", transform=ax.transAxes, fontsize=7,
            bbox={"facecolor": "white", "alpha": 0.8, "linewidth": 0})
    fig.savefig(path, dpi=90, bbox_inches="tight")
    plt.close(fig)
