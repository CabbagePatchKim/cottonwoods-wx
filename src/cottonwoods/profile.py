"""A model's temperature/humidity profile at a station's REAL elevation, and what falls there: snow, mix or rain.

Why: a model's ground is rarely at the station. GFS puts the ground near Alta at ~6,700 ft, so its "warmest
layer" can be valley air 2,000 ft below the station, and Kuchera turns that into a 3:1 ratio. HRRR's ground is
close to the station. Interpolating every model's profile to the station's elevation puts them on equal terms.

Phase uses WET-BULB temperature (Tw), not air temperature: snow falling into dry air cools it by evaporation, so
snow can fall and stick with the thermometer at +1 to +3 C. Following NWS Western Region (Van Cleave et al.,
NWS WR Technical Attachment 19-01), based on 10 years of Salt Lake City soundings vs observed precip type:
  - Snow level = the height of the 0.5 C wet-bulb (the NWS WR / National Blend standard).
  - At the station: Tw <= mix_low_tw_c (0 C) snow; >= mix_high_tw_c (1 C) rain; in between a mix, with the snow
    fraction falling linearly (SLC saw mixed precip mostly at surface Tw 0-1 C).
  - Dry air below the cloud: evaporative cooling has limits (light precip can't cool a deep dry layer all the way
    to its wet-bulb), so NWS nudges the snow level toward the freezing level. Here: mean RH from the station up to
    the freezing level; no nudge at rh_moist (80%) or wetter, rising to max_nudge (half the way) at rh_dry (40%).
    The same fraction moves the station's wet-bulb toward its air temperature. A simple first version.
All values live in config precip_type: and are starting points, to be fitted to Alta observations later.

Inputs are the per-step columns the pullers save: psfc_hpa, t_<lev>_k, rh_<lev> (%), z_<lev>_m (geopotential
height; may be given only at 500 mb, the rest is then filled in with the hypsometric equation), t2m_k, td2m_k.
"""
from __future__ import annotations

import numpy as np

RD_G = 287.05 / 9.80665          # gas constant / gravity: metres of thickness per kelvin per unit ln(p)
LAPSE = 0.0065                   # K per m, to reach a station BELOW the model's ground
TD_LAPSE = 0.0018                # dewpoint falls ~1.8 K per km


def e_sat(t_c):
    """Saturation vapor pressure over water (hPa), Bolton (1980)."""
    return 6.112 * np.exp(17.67 * np.asarray(t_c, float) / (np.asarray(t_c, float) + 243.5))


def dewpoint_c(t_c, rh):
    """Dewpoint (C) from temperature (C) and relative humidity (%)."""
    e = e_sat(t_c) * np.clip(np.asarray(rh, float), 1, 100) / 100.0
    x = np.log(e / 6.112)
    return 243.5 * x / (17.67 - x)


def wet_bulb_c(t_c, td_c, p_hpa):
    """Wet-bulb temperature (C), solving the psychrometric equation by bisection between Td and T.

    e_sat(Tw) - A * p * (T - Tw) = e,  A = 6.6e-4 * (1 + 0.00115 * Tw)  (works on arrays; ~0.01 C precision)
    Uses pressure, so it's right at mountain elevations (a sea-level formula is ~0.3 C off at 750 mb).
    """
    t, td, p = (np.asarray(x, float) for x in (t_c, td_c, p_hpa))
    e = e_sat(td)
    lo, hi = np.minimum(td, t), t.copy()
    for _ in range(40):
        mid = (lo + hi) / 2
        f = e_sat(mid) - 6.6e-4 * (1 + 0.00115 * mid) * p * (t - mid) - e
        hi = np.where(f > 0, mid, hi)
        lo = np.where(f > 0, lo, mid)
    return (lo + hi) / 2


def level_heights(levels: list[int], temps_k: dict, z_given: dict) -> dict:
    """Geopotential height (m) of each level. Uses heights the model gave; fills the rest from the nearest known
    level with the hypsometric equation (thickness = Rd/g * mean T * ln(p_lower/p_upper))."""
    z = {lev: z_given[lev] for lev in levels if lev in z_given and np.isfinite(z_given[lev])}
    if not z:
        return {}
    order = sorted(levels)                         # top (lowest pressure) first
    for _ in range(len(order)):                    # sweep until every level is filled
        for a, b in zip(order, order[1:]):         # a above b
            if a in z and b not in z and np.isfinite(temps_k.get(a, np.nan)) and np.isfinite(temps_k.get(b, np.nan)):
                z[b] = z[a] - RD_G * (temps_k[a] + temps_k[b]) / 2 * np.log(b / a)
            if b in z and a not in z and np.isfinite(temps_k.get(a, np.nan)) and np.isfinite(temps_k.get(b, np.nan)):
                z[a] = z[b] + RD_G * (temps_k[a] + temps_k[b]) / 2 * np.log(b / a)
    return z


def _crossing(zs, vals, value):
    """Height where `vals` (ordered bottom to top) crosses `value`, searching from the top down; NaN if never."""
    for i in range(len(zs) - 1, 0, -1):
        if np.isfinite(vals[i]) and np.isfinite(vals[i - 1]) and vals[i] < value <= vals[i - 1]:
            return zs[i - 1] + (zs[i] - zs[i - 1]) * (value - vals[i - 1]) / (vals[i] - vals[i - 1])
    return np.nan


def station_profile(row: dict, levels: list[int], z_station_m: float | None, pcfg: dict,
                    top_hpa: float = 500) -> dict:
    """Profile values at the station for one model step. Returns {} when the row lacks the needed fields.

    z_station_m None (e.g. a GFS grid cell with no real site) means "use the model's own ground".
    pcfg: config precip_type (thresholds and the dry-air nudge; see the module docstring). No defaults here:
    the config is the only source of these values.
    """
    c = pcfg
    psfc, t2, td2 = row.get("psfc_hpa"), row.get("t2m_k"), row.get("td2m_k")
    temps = {lev: row.get(f"t_{lev}_k", np.nan) for lev in levels}
    z = level_heights(levels, temps, {lev: row.get(f"z_{lev}_m", np.nan) for lev in levels})
    if not (np.isfinite(psfc) and np.isfinite(t2) and np.isfinite(td2) and z):
        return {}
    above = [lev for lev in sorted(levels, reverse=True) if lev < psfc and lev in z and np.isfinite(temps[lev])]
    if not above:
        return {}
    # Model ground height: extend from the lowest above-ground level down to the surface pressure
    low = above[0]
    z_ground = z[low] - RD_G * (temps[low] + t2) / 2 * np.log(psfc / low)
    # The profile from the ground up: (height, pressure, T, Td), with the 2 m point at the bottom
    pts = [(z_ground + 2, psfc, t2 - 273.15, td2 - 273.15)]
    for lev in above:
        rh = row.get(f"rh_{lev}", np.nan)
        t_c = temps[lev] - 273.15
        pts.append((z[lev], lev, t_c, dewpoint_c(t_c, rh) if np.isfinite(rh) else np.nan))
    zs, ps, ts, tds = (np.array(v, float) for v in zip(*pts))
    good = np.isfinite(tds)
    zs_s = z_ground + 2 if z_station_m is None else z_station_m

    if zs_s >= zs[0]:
        t_s, p_s = np.interp(zs_s, zs, ts), np.exp(np.interp(zs_s, zs, np.log(ps)))
        td_s = np.interp(zs_s, zs[good], tds[good]) if good.sum() >= 2 else tds[0]
    else:  # station below the model's ground (rare: HRRR at Alta base): extend downward at standard lapse rates
        dz = zs[0] - zs_s
        t_s, td_s = ts[0] + LAPSE * dz, tds[0] + TD_LAPSE * dz
        p_s = ps[0] * np.exp(dz / (RD_G * (ts[0] + 273.15 + LAPSE * dz / 2)))
    td_s = min(td_s, t_s)
    tw_s = float(wet_bulb_c(t_s, td_s, p_s))

    # Kuchera's warmest layer, but only from the STATION up to top_hpa (air below the station doesn't count)
    col = [t_s] + [t for zz, p, t in zip(zs, ps, ts) if zz > zs_s and p >= top_hpa]
    tmax_k = max(col) + 273.15

    # Freezing level (air 0 C) and snow level (wet-bulb 0.5 C), coming down from aloft
    tw = np.where(good, wet_bulb_c(ts, np.minimum(np.where(good, tds, ts), ts), ps), np.nan)
    fzl = _crossing(zs, ts, 0.0)
    snow_level = _crossing(zs, tw, c["snow_level_tw_c"])
    if np.isnan(snow_level) and np.nanmax(tw) < c["snow_level_tw_c"]:
        snow_level = zs[0]                         # snow all the way to the model's ground
    if np.isnan(fzl) and np.nanmax(ts) < 0:
        fzl = zs[0]

    # Dry air below the cloud: nudge part of the way from wet-bulb back toward air temperature
    top = max(fzl, zs_s) if np.isfinite(fzl) else zs_s
    layer = [100 * e_sat(td) / e_sat(t) for zz, t, td in zip(zs, ts, tds)
             if np.isfinite(td) and zs_s <= zz <= top]
    rh_sub = float(np.mean(layer + [100 * e_sat(td_s) / e_sat(t_s)]))
    nudge = c["max_nudge"] * float(np.clip((c["rh_moist"] - rh_sub) / (c["rh_moist"] - c["rh_dry"]), 0, 1))
    tw_eff = tw_s + nudge * (t_s - tw_s)
    if np.isfinite(snow_level) and np.isfinite(fzl) and fzl > snow_level:
        snow_level = snow_level + nudge * (fzl - snow_level)

    lo, hi = c["mix_low_tw_c"], c["mix_high_tw_c"]
    frac = float(np.clip((hi - tw_eff) / (hi - lo), 0, 1))
    return {"z_ground_m": z_ground, "t_stn_c": t_s, "tw_stn_c": tw_s, "tw_eff_c": tw_eff, "rh_sub": rh_sub,
            "dry_nudge": nudge, "tmax_stn_k": tmax_k, "freezing_level_m": fzl, "snow_level_m": snow_level,
            "snow_frac": frac}
