"""Storm events: detect them in model runs and observations, keep one storm table, verify when a storm is over.

Rules (owner's, config `storms:`):
  - A storm starts at the first step with measurable precip (wet_in, 0.01") and ends at the last wet step before
    a dry gap of gap_hours (12 h). Short breaks, like lake-effect bands behind a front, stay part of the storm.
    It must total min_total_in (0.10") to count.
  - The storm's timeframe is the LONGEST event among the newest run of each deterministic model, and every model
    is totalled over that same window. (Ensemble means smear precip in time, so ensembles don't set the window.)
  - Verification waits until the observations (SNOTEL hourly gauge at the point's sites) show the storm is over:
    gap_hours with no measurable precip after the last wet hour. The final window is widened to cover the
    observed event if it started earlier or ended later than forecast.

Flow regimes change through a storm, so they're kept as PHASES (cottonwoods.regimes.phases), e.g.
"SW warm 65% -> NW cold 35%": fc_flow from the run that defines the window, obs_flow from the Salt Lake City
balloons once verified, and every scored run's own phases in storm_scores. Resort snow in the report is scaled
step by step by that step's regime factor (data/processed/regime_factors.csv, scripts/regime_factors.py).

A tracked storm's status: forecast -> verified, or
  dropped  a FUTURE storm no model's newest run shows any more (storms already under way are never dropped: they're
           waiting for verification, and newer runs naturally don't show precip that has already fallen)
  merged   one model event now spans it and an earlier storm: it's folded into that one ("merged into <id>")
A storm that newer runs show as two separate events keeps the first; the other becomes a new storm ("split from
<id>"). The report also lists, per storm, models that show it as separate periods, and its sub-periods (gaps of
storms.subperiod_gap_hours or more inside the defining run), so a two-part storm like the journal's 2/16 a/b
stays visible.

data/archive/storms.csv            one row per storm (status: forecast -> verified / dropped / merged)
data/archive/storm_scores.csv      one row per storm x model run (x member for ensembles) once verified
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import modes, regimes, timefmt
from .windows import window_total
from .archive.io import write_df
from .config import ARCHIVE_DIR

MM_PER_IN = 25.4
COLS = ["storm_id", "point", "status", "fc_start", "fc_end", "defining_model", "defining_init", "outside_note",
        "fc_flow", "fc_modes", "fc_periods", "fc_subperiods", "obs_start", "obs_end", "final_start", "final_end",
        "obs_water_in", "obs_flow", "note", "updated_at"]
TEXT_COLS = ("storm_id", "point", "status", "defining_model", "outside_note", "fc_flow", "fc_modes", "fc_periods",
             "fc_subperiods", "obs_flow", "note")
TIME_COLS = ("fc_start", "fc_end", "defining_init", "obs_start", "obs_end", "final_start", "final_end", "updated_at")


# --- detection -------------------------------------------------------------------------------

def detect_events(steps: pd.DataFrame, wet_mm: float, gap_hours: float, min_total_mm: float) -> pd.DataFrame:
    """Storm events in one precip series (columns valid_start, valid_end, qpf_mm), as start, end, total_mm.

    A step is wet if its precip >= wet_mm. Wet steps separated by less than gap_hours of dry time belong to
    the same event; the dry time is measured from the end of one wet step to the start of the next.
    """
    # tiny tolerance: a gauge's 0.1" step computed as a difference of totals can come out as 0.0999999"
    wet = steps[steps["qpf_mm"] >= wet_mm - 1e-6].sort_values("valid_start")
    events, cur = [], None
    for _, r in wet.iterrows():
        if cur and (r.valid_start - cur["end"]) < pd.Timedelta(hours=gap_hours):
            cur["end"] = max(cur["end"], r.valid_end)          # still the same storm: extend it
        else:
            if cur:
                events.append(cur)
            cur = {"start": r.valid_start, "end": r.valid_end}  # a 12 h dry gap (or the first wet step) starts a new one
    if cur:
        events.append(cur)
    out = []
    for ev in events:
        total = window_total(steps, ev["start"], ev["end"])
        if total >= min_total_mm:
            out.append({**ev, "total_mm": total})
    return pd.DataFrame(out, columns=["start", "end", "total_mm"])


def obs_steps(obs: pd.DataFrame, sites: list[str]) -> pd.DataFrame:
    """Hourly observed precip steps from SNOTEL gauge totals (PREC) at the given sites, averaged across sites.

    The gauge reports a running total in 0.1" steps; each hourly rise is that hour's precip. Drops (gauge
    resets, noise) count as 0.
    """
    parts = []
    for site in sites:
        s = obs[(obs["site"] == site) & (obs["element"] == "PREC")].sort_values("time_utc")
        s = s.drop_duplicates("time_utc", keep="last").set_index("time_utc")["value"]
        if len(s) > 1:
            parts.append(s.diff().clip(lower=0).rename(site))
    if not parts:
        return pd.DataFrame(columns=["valid_start", "valid_end", "qpf_mm"])
    inc = pd.concat(parts, axis=1).mean(axis=1).dropna()
    return pd.DataFrame({"valid_start": inc.index - pd.Timedelta(hours=1), "valid_end": inc.index,
                         "qpf_mm": inc.to_numpy() * MM_PER_IN}).reset_index(drop=True)


# --- forecast storm windows ------------------------------------------------------------------

def model_events(fc: pd.DataFrame, point: str, scfg: dict) -> pd.DataFrame:
    """Events in the newest run of each deterministic model at one point: model, init, start, end, total_mm."""
    fc = fc[fc["point"] == point]
    if "member" in fc.columns:
        fc = fc[fc["member"].isna()]
    rows = []
    for model, g in fc.groupby("model"):
        g = g[g["init"] == g["init"].max()]
        ev = detect_events(g, scfg["wet_in"] * MM_PER_IN, scfg["gap_hours"], scfg["min_total_in"] * MM_PER_IN)
        for _, e in ev.iterrows():
            rows.append({"model": model, "init": g["init"].iloc[0], **e.to_dict()})
    return pd.DataFrame(rows, columns=["model", "init", "start", "end", "total_mm"])


def storm_windows(events: pd.DataFrame) -> list[dict]:
    """Group overlapping model events into storms; each storm's window is its LONGEST single model event.

    outside_note lists any model whose event pokes outside that window (worth knowing, not used to widen it).
    fc_periods lists models that show the storm as two or more separate events (they overlap the same group, e.g.
    ECMWF splits what GFS shows as one long event), with Mountain times.
    """
    if events.empty:
        return []
    ev = events.sort_values("start").reset_index(drop=True)
    groups, cur, cur_end = [], [], None
    for i, r in ev.iterrows():
        if cur and r.start <= cur_end:          # overlaps the current group -> same storm
            cur.append(i)
            cur_end = max(cur_end, r.end)
        else:
            if cur:
                groups.append(cur)
            cur, cur_end = [i], r.end
    groups.append(cur)
    out = []
    for idx in groups:
        g = ev.loc[idx]
        longest = g.loc[(g.end - g.start).idxmax()]
        outside = [f"{r.model} {'starts' if r.start < longest.start else 'ends'} "
                   f"{abs((r.start - longest.start) if r.start < longest.start else (r.end - longest.end)) / pd.Timedelta(hours=1):.0f} h "
                   f"{'earlier' if r.start < longest.start else 'later'}"
                   for _, r in g.iterrows() if r.start < longest.start or r.end > longest.end]
        periods = [f"{model} shows {len(m)} separate periods: "
                   + "; ".join(f"{timefmt.clock(r.start, weekday=True)}-{timefmt.clock(r.end, weekday=True)}"
                               for _, r in m.sort_values("start").iterrows())
                   for model, m in g.groupby("model") if len(m) > 1]
        out.append({"fc_start": longest.start, "fc_end": longest.end, "defining_model": longest.model,
                    "defining_init": longest.init, "outside_note": "; ".join(outside), "fc_periods": " | ".join(periods)})
    return out


def run_flow(g: pd.DataFrame, point: str, s, e, cfg: dict) -> str:
    """Regime phases of one run at one point over [s, e] (wet steps only), as text; '' if the run lacks wind."""
    gp = g[(g["point"] == point) & (g["valid_end"] > s) & (g["valid_start"] < e)]
    if "regime" not in gp.columns:
        return ""
    wet = gp[gp["qpf_mm"] >= cfg["storms"]["wet_in"] * MM_PER_IN]
    return regimes.phase_string(regimes.phases(wet, cfg["regimes"]))


def run_modes(g: pd.DataFrame, point: str, s, e, cfg: dict) -> str:
    """Precip modes of one run at one point over [s, e] (wet steps only), e.g. 'frontal 60% -> post-frontal 40%'."""
    gp = g[(g["point"] == point) & (g["valid_end"] > s) & (g["valid_start"] < e)]
    if "precip_mode" not in gp.columns:
        return ""
    return modes.mode_string(gp[gp["qpf_mm"] >= cfg["storms"]["wet_in"] * MM_PER_IN])


def run_subperiods(g: pd.DataFrame, point: str, s, e, cfg: dict) -> str:
    """Parts of the storm in one run separated by storms.subperiod_gap_hours or more without measurable precip:
    'Sat 10/31 0300 MDT-Sat 10/31 2100 MDT 70% | Sun 11/01 0900 MST-Mon 11/02 1100 MST 30%' ('' if one part)."""
    scfg = cfg["storms"]
    gp = g[(g["point"] == point) & (g["valid_end"] > s) & (g["valid_start"] < e)]
    parts = detect_events(gp, scfg["wet_in"] * MM_PER_IN, scfg["subperiod_gap_hours"], 0.0)
    if len(parts) < 2:
        return ""
    total = parts["total_mm"].sum() or 1.0
    return " | ".join(f"{timefmt.clock(r.start, weekday=True)}-{timefmt.clock(r.end, weekday=True)} "
                      f"{100 * r.total_mm / total:.0f}%" for _, r in parts.iterrows())


# --- storm table -----------------------------------------------------------------------------

def storms_path(root=None):
    return (root or ARCHIVE_DIR) / "storms.csv"


def load_storms(root=None) -> pd.DataFrame:
    p = storms_path(root)
    if not p.exists():
        return pd.DataFrame(columns=COLS)
    # Text columns read as text (an all-blank column would otherwise load as numbers and refuse a note later)
    df = pd.read_csv(p, dtype={c: object for c in TEXT_COLS})
    for c in COLS:
        if c not in df.columns:  # storms saved before a column existed
            df[c] = pd.Series([None] * len(df), dtype=object) if c in TEXT_COLS else np.nan
    for c in TEXT_COLS:
        df[c] = df[c].astype(object)
    for c in TIME_COLS:
        df[c] = pd.to_datetime(df[c])
    return df


def update_forecast_storms(storms: pd.DataFrame, fc: pd.DataFrame, cfg: dict, now: pd.Timestamp) -> pd.DataFrame:
    """Refresh tracked storms from the newest runs: update, add, split, merge and drop (see the module docstring)."""
    scfg = cfg["storms"]
    point = scfg["point"]
    storms = storms.copy()
    touched: set = set()   # rows updated or created in this pass
    # Overlaps are judged against each storm's window as it was BEFORE this pass: updating a storm from the first
    # event must not hide that a second event also overlaps it (that's a split, not a new unrelated storm).
    before = storms[["fc_start", "fc_end"]].copy()

    def blank_row(w, **extra):
        row = {c: pd.NaT if c in TIME_COLS else np.nan for c in COLS}
        row.update(w, storm_id=f"{w['fc_start']:%Y%m%d%H}", point=point, status="forecast", updated_at=now, **extra)
        return row

    windows = [w for w in storm_windows(model_events(fc, point, scfg)) if w["fc_end"] >= now - pd.Timedelta(days=3)]
    for w in sorted(windows, key=lambda x: x["fc_start"]):
        g = fc[(fc["model"] == w["defining_model"]) & (fc["init"] == w["defining_init"])]
        w["fc_flow"] = run_flow(g, point, w["fc_start"], w["fc_end"], cfg)
        w["fc_modes"] = run_modes(g, point, w["fc_start"], w["fc_end"], cfg)
        w["fc_subperiods"] = run_subperiods(g, point, w["fc_start"], w["fc_end"], cfg)
        is_open = (storms["point"] == point) & (storms["status"] == "forecast") & storms.index.isin(before.index)
        span = before.reindex(storms.index)
        hits = storms[is_open & (span["fc_start"] <= w["fc_end"]) & (span["fc_end"] >= w["fc_start"])]
        hits = hits.loc[span.loc[hits.index, "fc_start"].sort_values().index]
        if hits.empty:
            already_verified = ((storms["point"] == point) & (storms["status"] == "verified")
                                & (storms["final_start"] <= w["fc_end"]) & (storms["final_end"] >= w["fc_start"])).any()
            if not already_verified:
                storms = pd.concat([storms, pd.DataFrame([blank_row(w)])], ignore_index=True)
                touched.add(storms.index[-1])
            continue
        keep = hits.index[0]
        if keep in touched:
            # this storm was already matched to an earlier event in this pass: newer runs show it as two events
            storms = pd.concat([storms, pd.DataFrame([blank_row(w, note=f"split from {storms.at[keep, 'storm_id']}")])],
                               ignore_index=True)
            touched.add(storms.index[-1])
            continue
        for k, v in w.items():
            storms.at[keep, k] = v
        storms.at[keep, "updated_at"] = now
        touched.add(keep)
        for other in hits.index[1:]:
            if other not in touched:   # one event now spans two tracked storms: fold the later one in
                storms.at[other, "status"] = "merged"
                storms.at[other, "note"] = f"merged into {storms.at[keep, 'storm_id']} ({timefmt.clock(now)})"
                storms.at[other, "updated_at"] = now

    # Future storms no model's newest run shows any more. (Storms already under way are kept for verification.)
    stale = (storms["point"] == point) & (storms["status"] == "forecast") & (storms["fc_start"] > now) \
        & ~storms.index.isin(touched)
    storms.loc[stale, "status"] = "dropped"
    storms.loc[stale, "note"] = f"no model's newest run shows it any more ({timefmt.clock(now)})"
    storms.loc[stale, "updated_at"] = now
    return storms


def ready_to_verify(storm: pd.Series, osteps: pd.DataFrame, cfg: dict, now: pd.Timestamp) -> dict | None:
    """Observed start/end if the storm is over (gap_hours dry after the last wet hour, and data through then)."""
    scfg = cfg["storms"]
    if osteps.empty or now < storm.fc_end:
        return None
    s0, e0 = storm.fc_start - pd.Timedelta(hours=scfg["gap_hours"]), storm.fc_end + pd.Timedelta(hours=48)
    ev = detect_events(osteps[(osteps.valid_end > s0) & (osteps.valid_start < e0)],
                       scfg["obs_wet_in"] * MM_PER_IN, scfg["gap_hours"], 0.0)
    ev = ev[(ev.start <= storm.fc_end + pd.Timedelta(hours=scfg["gap_hours"])) & (ev.end >= s0)]
    last_obs = osteps["valid_end"].max()
    if ev.empty:  # nothing observed: a forecast bust once we have data well past the window
        return {"obs_start": pd.NaT, "obs_end": pd.NaT} if last_obs >= storm.fc_end + pd.Timedelta(hours=scfg["gap_hours"]) else None
    obs_end = ev["end"].max()
    if last_obs < obs_end + pd.Timedelta(hours=scfg["gap_hours"]):
        return None  # still snowing, or not dry long enough yet
    return {"obs_start": ev["start"].min(), "obs_end": obs_end}


def score_storm(storm: pd.Series, fc_all: pd.DataFrame, osteps: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Every archived run that started before the storm ended, totalled over the final window, vs observed."""
    s, e = storm.final_start, storm.final_end
    obs_mm = window_total(osteps, s, e)
    thr = cfg["verify"]["event_swe_in"] * MM_PER_IN
    fc = fc_all[(fc_all["point"] == storm.point) & (fc_all["init"] < e)]
    rows = []
    keys = ["model", "init"] + (["member"] if "member" in fc.columns else [])
    for key, g in fc.groupby(keys, dropna=False):
        if g["valid_end"].max() < e or g["valid_start"].min() > s:
            continue  # this run doesn't cover the whole window (too short, or started mid-storm)
        model, init = key[0], key[1]
        member = key[2] if len(key) > 2 else np.nan
        water = window_total(g, s, e) / MM_PER_IN
        snow = window_total(g, s, e, "snow_in") if "snow_in" in g.columns and g["snow_in"].notna().any() else np.nan
        rows.append({"storm_id": storm.storm_id, "point": storm.point, "model": model, "init": init, "member": member,
                     "lead_to_start_hr": (s - init) / pd.Timedelta(hours=1), "fc_water_in": water,
                     "fc_snow_in": snow, "obs_water_in": obs_mm / MM_PER_IN,
                     "fc_event": water * MM_PER_IN >= thr, "obs_event": obs_mm >= thr,
                     "fc_flow": run_flow(g, storm.point, s, e, cfg), "fc_modes": run_modes(g, storm.point, s, e, cfg),
                     "obs_flow": storm.get("obs_flow", "")})
    return pd.DataFrame(rows)


def append_scores(scores: pd.DataFrame, root=None) -> None:
    p = (root or ARCHIVE_DIR) / "storm_scores.csv"
    if p.exists():
        old = pd.read_csv(p)
        old = old[~old["storm_id"].astype(str).isin(scores["storm_id"].astype(str))]  # re-scoring replaces
        scores = pd.concat([old, scores], ignore_index=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    scores.to_csv(p, index=False)


def run(fc: pd.DataFrame, obs: pd.DataFrame, cfg: dict, now: pd.Timestamp, root=None,
        soundings: pd.DataFrame | None = None) -> pd.DataFrame:
    """One pass: update forecast storms from the newest runs, verify any that are over. Returns the table.

    soundings: balloon summaries (cottonwoods.obs.sounding.summarize) for the observed regime phases.
    """
    storms = update_forecast_storms(load_storms(root), fc, cfg, now)
    point = cfg["storms"]["point"]
    sites = [s for s in (cfg["points"][point].get("obs") or []) if s in cfg["snotel"]]
    osteps = obs_steps(obs, sites) if not obs.empty else pd.DataFrame(columns=["valid_start", "valid_end", "qpf_mm"])
    for i, st in storms[storms["status"] == "forecast"].iterrows():
        done = ready_to_verify(st, osteps, cfg, now)
        if done is None:
            continue
        storms.loc[i, ["obs_start", "obs_end"]] = [done["obs_start"], done["obs_end"]]
        storms.at[i, "final_start"] = min(st.fc_start, done["obs_start"]) if pd.notna(done["obs_start"]) else st.fc_start
        storms.at[i, "final_end"] = max(st.fc_end, done["obs_end"]) if pd.notna(done["obs_end"]) else st.fc_end
        storms.at[i, "obs_water_in"] = window_total(osteps, storms.at[i, "final_start"], storms.at[i, "final_end"]) / MM_PER_IN
        if soundings is not None and not soundings.empty:
            ss = regimes.sounding_steps(soundings, storms.at[i, "final_start"], storms.at[i, "final_end"], cfg["regimes"])
            storms.at[i, "obs_flow"] = regimes.phase_string(regimes.phases(ss, {**cfg["regimes"], "min_phase_hours": 12}, "w"))
        storms.at[i, "status"] = "verified"
        storms.at[i, "updated_at"] = now
        append_scores(score_storm(storms.loc[i], fc, osteps, cfg), root)
    write_df(storms[COLS], storms_path(root))
    return storms
