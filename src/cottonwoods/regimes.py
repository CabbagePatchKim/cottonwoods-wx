"""Flow-regime labels for a storm from 700 mb conditions.

Inputs are event-mean values (from ERA5 or a model analysis) near the Wasatch.
Thresholds live in config/stations.yaml and should be tuned, not trusted.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from .windows import window_total


def wind_dir_deg(u, v):
    """Meteorological wind direction (degrees the wind blows FROM) from u/v components."""
    # u = west-to-east wind, v = south-to-north wind. arctan2 gives the angle the wind blows TOWARD;
    # negating both flips it to where it comes FROM. "+ 360 then % 360" maps -90 onto 270 etc.
    return (np.degrees(np.arctan2(-np.asarray(u, float), -np.asarray(v, float))) + 360.0) % 360.0


def wind_speed(u, v):
    # hypot = sqrt(u**2 + v**2), the length of the wind vector
    return np.hypot(np.asarray(u, float), np.asarray(v, float))


# --- per-step regimes and storm phases ------------------------------------------------------------
# Regimes change through a storm (SW ahead of the front, W/NW behind it, maybe lake effect at the end), so each
# model step and each balloon sounding gets its own label, and a storm is summarized as a sequence of phases.

def flow_label(dir_deg: float, cfg: dict) -> str:
    """'SW', 'W', 'NW', 'N' or cfg other_label, from config regimes.flow_bins ([from, to), wrapping past north)."""
    if dir_deg is None or not np.isfinite(dir_deg):
        return "?"
    for lo, hi, name in cfg["flow_bins"]:
        inside = lo <= dir_deg < hi if lo < hi else (dir_deg >= lo or dir_deg < hi)
        if inside:
            return name
    return cfg["other_label"]


def thermal_label(t700_c: float, cfg: dict) -> str:
    """The two-way split (cold at or below cold_700mb_c, else warm). Kept for the regime factors' middle fallback
    level; what people see is band_label."""
    return "?" if t700_c is None or not np.isfinite(t700_c) else ("cold" if t700_c <= cfg["cold_700mb_c"] else "warm")


def band_label(t700_c: float, cfg: dict) -> str:
    """The displayed temperature band from config temp_bands_700mb_c: [[above, name], ...], first band whose
    `above` the temperature exceeds; `above` null = everything colder. Default: warm > -4, mild > -10, cold."""
    if t700_c is None or not np.isfinite(t700_c):
        return "?"
    for above, name in cfg["temp_bands_700mb_c"]:
        if above is None or t700_c > above:
            return name
    return "?"


def add_step_regimes(df, cfg: dict):
    """Add flow_dir700, flow_spd700_kt, flow, thermal (2-way), band (3-way) and regime ('SW mild') to each row that
    has 700 mb wind."""
    if not {"u_700_ms", "v_700_ms"} <= set(df.columns):
        return df
    out = df.copy()
    out["flow_dir700"] = wind_dir_deg(out["u_700_ms"], out["v_700_ms"])
    out["flow_spd700_kt"] = wind_speed(out["u_700_ms"], out["v_700_ms"]) * 1.944
    t700 = out["t_700_k"] - 273.15 if "t_700_k" in out else np.nan
    out["flow"] = [flow_label(d, cfg) for d in out["flow_dir700"]]
    out["thermal"] = [thermal_label(t, cfg) for t in np.broadcast_to(t700, len(out))]
    out["band"] = [band_label(t, cfg) for t in np.broadcast_to(t700, len(out))]
    out["regime"] = [f"{f} {b}" for f, b in zip(out["flow"], out["band"])]   # row by row: also works with no rows
    return out


def with_bands(df, cfg: dict):
    """df labelled with the current bands: runs archived before bands existed (or with other band edges) carry an
    older `regime`, so labels are recomputed from their stored 700 mb wind and temperature when read."""
    if not {"u_700_ms", "v_700_ms", "t_700_k"} <= set(df.columns):
        return df
    return add_step_regimes(df, cfg)


def phases(steps, cfg: dict, weight_col: str = "qpf_mm") -> list[dict]:
    """A storm's regime phases from its steps (valid_start, valid_end, regime, weight, flow_dir700, t_700_k).

    Consecutive steps with the same regime form a phase. A phase shorter than min_phase_hours is folded into the
    neighbor holding more water, repeatedly, so one odd step doesn't count as a regime change. Each phase:
    regime, start, end, water share (0-1), water-weighted 700 mb direction and temperature.
    """
    if steps.empty or "regime" not in steps:
        return []
    s = steps.sort_values("valid_start")
    s = s[s["regime"].notna() & ~s["regime"].astype(str).str.contains(r"\?", regex=True)]
    if s.empty:
        return []
    groups = []
    for _, r in s.iterrows():
        if groups and groups[-1]["regime"] == r.regime:
            groups[-1]["rows"].append(r)
        else:
            groups.append({"regime": r.regime, "rows": [r]})
    min_h = pd.Timedelta(hours=cfg["min_phase_hours"])

    def span(g):
        return g["rows"][-1].valid_end - g["rows"][0].valid_start

    def water(g):
        return sum(max(float(r[weight_col]), 0.0) for r in g["rows"])

    while len(groups) > 1:
        short = [i for i, g in enumerate(groups) if span(g) < min_h]
        if not short:
            break
        i = min(short, key=lambda k: water(groups[k]))       # fold the least important short phase first
        nbrs = [j for j in (i - 1, i + 1) if 0 <= j < len(groups)]
        j = max(nbrs, key=lambda k: water(groups[k]))
        a, b = sorted((i, j))
        keep = groups[j]["regime"]
        groups[a:b + 1] = [{"regime": keep, "rows": groups[a]["rows"] + groups[b]["rows"]}]
        # merging can join two neighbors with the same regime
        merged = [groups[0]]
        for g in groups[1:]:
            if g["regime"] == merged[-1]["regime"]:
                merged[-1] = {"regime": g["regime"], "rows": merged[-1]["rows"] + g["rows"]}
            else:
                merged.append(g)
        groups = merged

    total = sum(water(g) for g in groups) or 1.0
    out = []
    for g in groups:
        rows = pd.DataFrame(g["rows"])
        w = rows[weight_col].clip(lower=0)
        wsum = w.sum()
        if "u_700_ms" in rows:
            u = (rows["u_700_ms"] * w).sum() / wsum if wsum else rows["u_700_ms"].mean()
            v = (rows["v_700_ms"] * w).sum() / wsum if wsum else rows["v_700_ms"].mean()
            d = float(wind_dir_deg(u, v))
        else:
            d = float(rows.get("flow_dir700", pd.Series([np.nan])).mean())
        t = float(((rows["t_700_k"] * w).sum() / wsum if wsum else rows["t_700_k"].mean()) - 273.15) if "t_700_k" in rows else np.nan
        out.append({"regime": g["regime"], "start": rows["valid_start"].min(), "end": rows["valid_end"].max(),
                    "share": water(g) / total, "dir700": d, "t700_c": t})
    return out


def phase_string(ph: list[dict]) -> str:
    """'SW mild -6C 65% -> NW cold -12C 35%': each phase's water-weighted 700 mb temperature included ('' when
    there are no phases)."""
    return " -> ".join(f"{p['regime']}" + (f" {int(round(p['t700_c']))}C" if np.isfinite(p["t700_c"]) else "")   # int: no "-0C"
                       + f" {100 * p['share']:.0f}%" for p in ph)


def sounding_steps(summary, start, end, cfg: dict):
    """Observed regime steps from balloon sounding summaries (one row per launch, cottonwoods.obs.sounding.summarize).

    Each sounding stands for the 12 h centered on its launch time; weighted equally (no precip at the balloon site).
    """
    s = summary[(summary["time_utc"] >= start - pd.Timedelta(hours=6)) & (summary["time_utc"] <= end + pd.Timedelta(hours=6))]
    if s.empty:
        return s
    u = -s["wspd700_kt"] / 1.944 * np.sin(np.radians(s["wdir700"]))
    v = -s["wspd700_kt"] / 1.944 * np.cos(np.radians(s["wdir700"]))
    out = pd.DataFrame({"valid_start": s["time_utc"] - pd.Timedelta(hours=6), "valid_end": s["time_utc"] + pd.Timedelta(hours=6),
                        "u_700_ms": u.to_numpy(), "v_700_ms": v.to_numpy(), "t_700_k": (s["t700_c"] + 273.15).to_numpy(),
                        "w": 1.0})
    out["regime"] = [f"{flow_label(d, cfg)} {band_label(t, cfg)}" for d, t in zip(s["wdir700"], s["t700_c"])]
    return out


def weighted_by_obs(steps: pd.DataFrame, osteps: pd.DataFrame | None) -> pd.DataFrame:
    """steps with weight `w` = observed precip (mm, osteps) inside each step, so the snowy hours set a storm's
    regime; equal weights when nothing was observed (a dry storm still gets the flow it had)."""
    out = steps.copy()
    w = ([window_total(osteps, s, e) for s, e in zip(out["valid_start"], out["valid_end"])]
         if osteps is not None and not osteps.empty and len(out) else [])
    out["w"] = w if sum(w) > 0 else 1.0
    return out


def rap_steps(rap: pd.DataFrame, point: str, start, end, cfg: dict) -> pd.DataFrame:
    """Hourly observed regime steps at a point from RAP analyses (archive.obs.load_rap): each analysis stands for the
    hour after it."""
    r = rap[(rap["point"] == point) & (rap["time_utc"] >= start) & (rap["time_utc"] < end)] if not rap.empty else rap
    if r.empty:
        return pd.DataFrame()
    steps = pd.DataFrame({"valid_start": r["time_utc"].to_numpy(), "valid_end": (r["time_utc"] + pd.Timedelta(hours=1)).to_numpy(),
                          "u_700_ms": r["u_700_ms"].to_numpy(), "v_700_ms": r["v_700_ms"].to_numpy(),
                          "t_700_k": r["t_700_k"].to_numpy()})
    return add_step_regimes(steps, cfg)


def observed_regime(start, end, point: str, rap: pd.DataFrame | None, snd: pd.DataFrame | None,
                    osteps: pd.DataFrame | None, cfg: dict, min_rap_share: float = 0.75) -> dict:
    """A window's observed regime phases, weighted by observed precip: from hourly RAP analyses at the point when
    they cover at least min_rap_share of its hours, else from the SLC balloons; the balloons' phases are always
    returned too, as the cross-check. {"flow", "source" ("rap" / "balloons" / ""), "balloons"}."""
    rcfg = cfg["regimes"]
    hours = max((pd.Timestamp(end) - pd.Timestamp(start)) / pd.Timedelta(hours=1), 1)
    rs = rap_steps(rap, point, start, end, rcfg) if rap is not None else pd.DataFrame()
    bs = sounding_steps(snd, start, end, rcfg) if snd is not None and not snd.empty else pd.DataFrame()
    balloons = phase_string(phases(weighted_by_obs(bs, osteps), {**rcfg, "min_phase_hours": 12}, "w")) if len(bs) else ""
    if len(rs) >= min_rap_share * hours:
        return {"flow": phase_string(phases(weighted_by_obs(rs, osteps), rcfg, "w")), "source": "rap", "balloons": balloons}
    return {"flow": balloons, "source": "balloons" if balloons else "", "balloons": balloons}


_PHASE = re.compile(r"(\S+ \S+)(?: -?\d+C)? (\d+)%")


def dominant(phase_text) -> str:
    """The regime holding the most water in a phase string: 'SW warm 1C 30% -> W warm -2C 70%' -> 'W warm'; '' if
    there are none (the first wins a tie)."""
    found = _PHASE.findall(phase_text) if isinstance(phase_text, str) else []
    return max(found, key=lambda f: int(f[1]))[0] if found else ""


def balloon_regime(snd, times, cfg: dict) -> dict | None:
    """Regime of a day from the balloons launched at `times` (e.g. the 12z and 00z that bracket it): the vector-mean
    700 mb wind's flow label, and the mean 700 mb temperature's thermal (2-way) and band (3-way) labels. snd:
    sounding summaries indexed by time_utc. None when no balloon at those times has a 700 mb wind."""
    s = snd.reindex(times)
    u = -s["wspd700_kt"] * np.sin(np.radians(s["wdir700"]))
    v = -s["wspd700_kt"] * np.cos(np.radians(s["wdir700"]))
    if not u.notna().any():
        return None
    t = s["t700_c"].mean()
    return {"flow": flow_label(float(wind_dir_deg(u.mean(), v.mean())), cfg), "thermal": thermal_label(t, cfg),
            "band": band_label(t, cfg)}


# --- resort factors by regime ----------------------------------------------------------------------

def load_factors(path) -> pd.DataFrame:
    """data/processed/regime_factors.csv: point, flow, thermal (2-way), band (3-way), factor, n_days. A row with
    both thermal and band '' is the flow-only factor. Files from before bands get an empty band column."""
    from pathlib import Path
    p = Path(path)
    cols = ["point", "flow", "thermal", "band", "factor", "n_days"]
    f = pd.read_csv(p, keep_default_na=False) if p.exists() else pd.DataFrame(columns=cols)
    if "band" not in f.columns:
        f["band"] = ""
    return f


def factor_for(factors: pd.DataFrame, point: str, flow: str, thermal: str, band: str, min_days: int) -> float:
    """Factor for one point and regime, the finest level with min_days wet days of history: flow + band (3-way),
    else flow + thermal (2-way), else flow only, else 1.0. (Alta's N-flow factor rests on 4 days of Atwater data:
    too few to use.)"""
    f = factors[(factors["point"] == point) & (factors["flow"] == flow) & (factors["n_days"].astype(float) >= min_days)]
    if "band" not in f.columns:
        f = f.assign(band="")
    for level in (f[(f["band"] == band) & (f["thermal"] == "")], f[(f["thermal"] == thermal) & (f["band"] == "")],
                  f[(f["thermal"] == "") & (f["band"] == "")]):
        if len(level):
            return float(level["factor"].iloc[0])
    return 1.0


def apply_factors(df, factors: pd.DataFrame, cfg: dict):
    """Add regime_factor and snow_in_adj / qpf_mm_adj per step: each step scaled by ITS regime's factor."""
    out = with_bands(df.copy(), cfg)
    if "flow" not in out or factors.empty:
        out["regime_factor"] = 1.0
    else:
        if "band" not in out:
            out["band"] = "?"
        # Look each distinct (point, flow, thermal, band) up once: a 20-day load has ~1M steps but a few dozen regimes
        keys = list(zip(out["point"], out["flow"], out["thermal"], out["band"]))
        table = {k: factor_for(factors, *k, cfg["factor_min_days"]) for k in set(keys)}
        out["regime_factor"] = [table[k] for k in keys]
    out["qpf_mm_adj"] = out["qpf_mm"] * out["regime_factor"]
    if "snow_in" in out:
        out["snow_in_adj"] = out["snow_in"] * out["regime_factor"]
    return out
