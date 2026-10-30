"""Storm events: detect them in model runs and observations, keep one storm table, verify when a storm is over.

Rules (owner's, config `storms:`):
  - A storm starts at the first step with measurable precip (wet_in, 0.01") and ends at the last wet step before
    a dry gap of gap_hours (12 h). Short breaks, like lake-effect bands behind a front, stay part of the storm.
    It must total min_total_in (0.10") to count.
  - The storm's timeframe is the LONGEST event among the newest run of each deterministic model, and every model
    is totalled over that same window. (Ensemble means smear precip in time, so ensembles don't set the window.)
  - Only steps within max_lead_hours (168 h) of a run's init count for detection and the reported totals, so
    day 8-10 noise doesn't start storms. Verification still scores every archived run, at any lead.
  - Verification waits until the observations (SNOTEL hourly gauge at the point's sites) show the storm is over:
    gap_hours with no measurable precip after the last wet hour. The final window is widened to cover the
    observed event if it started earlier or ended later than forecast.

Flow regimes change through a storm, so they're kept as PHASES (cottonwoods.regimes.phases), e.g.
"SW warm 65% -> NW cold 35%": fc_flow from the run that defines the window; once verified, obs_flow from hourly RAP
analyses weighted by observed precip (obs_flow_source "rap"; the SLC balloons when RAP is missing), with the
balloons' own phases kept as the cross-check (obs_flow_balloon); every scored run's own phases in storm_scores.
Resort snow in the report is scaled
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
data/archive/storm_scores.csv      one row per storm x point x model run (x member for ensembles) once verified,
                                   at every point with a SNOTEL gauge (point_steps); the storm's point decides when
data/archive/block_scores.csv      the same storms in 12 h blocks (blocks.py): every deterministic run that covered a
                                   whole block, issued before it began, vs that block's observed water, so short-range
                                   models are scored on long storms (owner, 2026-10-29)
data/processed/bin_scores.csv      the deterministic runs of those with the owner's bin score, once that point has an
                                   observed snow total (hand entries, or 0 when its gauge was dry), with the event
                                   call and the snow / water error; rebuilt every pass
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import bins, blocks, modes, regimes, timefmt
from .models.registry import deterministic
from .windows import window_total, within_lead
from .archive.io import write_df
from .gauge import gauge_increments
from .config import ARCHIVE_DIR, PROCESSED_DIR
from .units import MM_PER_IN

COLS = ["storm_id", "point", "status", "fc_start", "fc_end", "defining_model", "defining_init", "outside_note",
        "fc_flow", "fc_modes", "fc_periods", "fc_subperiods", "obs_start", "obs_end", "final_start", "final_end",
        "obs_water_in", "obs_flow", "obs_flow_source", "obs_flow_balloon", "note", "updated_at"]
TEXT_COLS = ("storm_id", "point", "status", "defining_model", "outside_note", "fc_flow", "fc_modes", "fc_periods",
             "fc_subperiods", "obs_flow", "obs_flow_source", "obs_flow_balloon", "note")
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


def obs_steps(obs: pd.DataFrame, sites: list[str], gcfg: dict) -> pd.DataFrame:
    """Hourly observed precip steps from SNOTEL gauge totals (PREC) at the given sites, averaged across sites.

    The gauge reports a noisy running total in 0.1" steps; cottonwoods.gauge turns it into real hourly precip
    (smoothed, rises above the highest level so far only, resets start over), so noise isn't counted as precip.
    """
    parts = []
    for site in sites:
        s = obs[(obs["site"] == site) & (obs["element"] == "PREC")].sort_values("time_utc")
        s = s.drop_duplicates("time_utc", keep="last").set_index("time_utc")["value"]
        if len(s) > 1:
            parts.append(gauge_increments(s, gcfg).rename(site))
    if not parts:
        return pd.DataFrame(columns=["valid_start", "valid_end", "qpf_mm"])
    inc = pd.concat(parts, axis=1).mean(axis=1).dropna()
    return pd.DataFrame({"valid_start": inc.index - pd.Timedelta(hours=1), "valid_end": inc.index,
                         "qpf_mm": inc.to_numpy() * MM_PER_IN}).reset_index(drop=True)


def point_steps(obs: pd.DataFrame, cfg: dict) -> dict[str, pd.DataFrame]:
    """{point: observed steps} for every point with SNOTEL gauge data among its obs sites (config points.*.obs)."""
    out = {}
    for p, v in cfg["points"].items():
        sites = [s for s in (v.get("obs") or []) if s in cfg["snotel"]]
        steps = obs_steps(obs, sites, cfg["gauge"]) if sites and not obs.empty else pd.DataFrame()
        if not steps.empty:
            out[p] = steps
    return out


# --- forecast storm windows ------------------------------------------------------------------

def model_events(fc: pd.DataFrame, point: str, scfg: dict) -> pd.DataFrame:
    """Events in the newest run of each deterministic model at one point, within scfg max_lead_hours of its init:
    model, init, start, end, total_mm."""
    fc = deterministic(fc[fc["point"] == point])          # guidance and ensembles never set a storm's window
    rows = []
    for model, g in fc.groupby("model"):
        g = within_lead(g[g["init"] == g["init"].max()], scfg["max_lead_hours"])
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
    wet = regimes.with_bands(gp[gp["qpf_mm"] >= cfg["storms"]["wet_in"] * MM_PER_IN], cfg["regimes"])
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


def _overlaps(storms: pd.DataFrame, start_col: str, end_col: str, s, e) -> pd.Series:
    return (storms[start_col] <= e) & (storms[end_col] >= s)


def _apply_window(storms: pd.DataFrame, i, w: dict, now, **fields) -> None:
    """Copy a model window's values onto tracked storm i (plus any extra fields), stamped now."""
    for k, v in {**w, **fields}.items():
        storms.at[i, k] = v
    storms.at[i, "updated_at"] = now


def _without_legacy_duplicates(storms: pd.DataFrame) -> pd.DataFrame:
    """Storms tracked before revival existed could leave a dropped row and a live row with the same id; keep the
    live one."""
    live_ids = set(storms.loc[storms["status"] != "dropped", "storm_id"])
    return storms[~((storms["status"] == "dropped") & storms["storm_id"].isin(live_ids))].reset_index(drop=True)


def _new_storm(storms: pd.DataFrame, w: dict, point: str, now, **extra) -> pd.DataFrame:
    """storms with a new forecast storm for window w appended; its id is the start time, made unique if taken."""
    base, ids = f"{w['fc_start']:%Y%m%d%H}", set(storms["storm_id"])
    sid = base if base not in ids else next(f"{base}{c}" for c in "bcdefghijk" if f"{base}{c}" not in ids)
    row = {c: pd.NaT if c in TIME_COLS else np.nan for c in COLS}
    row.update(w, storm_id=sid, point=point, status="forecast", updated_at=now, **extra)
    return pd.concat([storms, pd.DataFrame([row])], ignore_index=True)


def _drop_stale(storms: pd.DataFrame, point: str, now, touched: set, horizon) -> None:
    """Future storms no model's newest run shows any more. (Storms already under way are kept for verification.)
    horizon: the furthest time any newest run is shown to (max_lead_hours); a storm starting past it is out of
    range, not gone, and comes back as the same storm once a run reaches it."""
    stale = (storms["point"] == point) & (storms["status"] == "forecast") & (storms["fc_start"] > now) \
        & ~storms.index.isin(touched)
    far = stale & (storms["fc_start"] >= horizon)
    storms.loc[stale, "status"] = "dropped"
    storms.loc[stale & ~far, "note"] = f"no model's newest run shows it any more ({timefmt.clock(now)})"
    storms.loc[far, "note"] = f"starts past {timefmt.clock(horizon)}, the lead limit of the newest runs ({timefmt.clock(now)})"
    storms.loc[stale, "updated_at"] = now


def update_forecast_storms(storms: pd.DataFrame, fc: pd.DataFrame, cfg: dict, now: pd.Timestamp) -> pd.DataFrame:
    """Refresh tracked storms from the newest runs: update, add, split, merge, revive and drop (see the module
    docstring)."""
    scfg = cfg["storms"]
    point = scfg["point"]
    storms = _without_legacy_duplicates(storms.copy())
    touched: set = set()   # rows updated or created in this pass
    # Overlaps are judged against each storm's window as it was BEFORE this pass: updating a storm from the first
    # event must not hide that a second event also overlaps it (that's a split, not a new unrelated storm).
    before = storms[["fc_start", "fc_end"]].copy()

    windows = [w for w in storm_windows(model_events(fc, point, scfg)) if w["fc_end"] >= now - pd.Timedelta(days=3)]
    for w in sorted(windows, key=lambda x: x["fc_start"]):
        g = fc[(fc["model"] == w["defining_model"]) & (fc["init"] == w["defining_init"])]
        w["fc_flow"] = run_flow(g, point, w["fc_start"], w["fc_end"], cfg)
        w["fc_modes"] = run_modes(g, point, w["fc_start"], w["fc_end"], cfg)
        w["fc_subperiods"] = run_subperiods(g, point, w["fc_start"], w["fc_end"], cfg)
        here = storms["point"] == point
        is_open = here & (storms["status"] == "forecast") & storms.index.isin(before.index)
        span = before.reindex(storms.index)
        hits = storms[is_open & _overlaps(span, "fc_start", "fc_end", w["fc_start"], w["fc_end"])]
        hits = hits.loc[span.loc[hits.index, "fc_start"].sort_values().index]
        if hits.empty:
            if (here & (storms["status"] == "verified")
                    & _overlaps(storms, "final_start", "final_end", w["fc_start"], w["fc_end"])).any():
                continue
            dropped = storms[here & (storms["status"] == "dropped") & ~storms.index.isin(touched)
                             & _overlaps(storms, "fc_start", "fc_end", w["fc_start"], w["fc_end"])]
            if len(dropped):
                # a storm the models dropped and now show again: the same storm, revived (not a new one)
                back = dropped.sort_values("updated_at").index[-1]
                _apply_window(storms, back, w, now, status="forecast",
                              note=f"back in the models after being dropped ({timefmt.clock(now)})")
                touched.add(back)
            else:
                storms = _new_storm(storms, w, point, now)
                touched.add(storms.index[-1])
            continue
        keep = hits.index[0]
        if keep in touched:
            # this storm was already matched to an earlier event in this pass: newer runs show it as two events
            storms = _new_storm(storms, w, point, now, note=f"split from {storms.at[keep, 'storm_id']}")
            touched.add(storms.index[-1])
            continue
        _apply_window(storms, keep, w, now)
        touched.add(keep)
        for other in hits.index[1:]:
            if other not in touched:   # one event now spans two tracked storms: fold the later one in
                _apply_window(storms, other, {}, now, status="merged",
                              note=f"merged into {storms.at[keep, 'storm_id']} ({timefmt.clock(now)})")
    det = fc[fc["member"].isna()] if "member" in fc.columns else fc
    horizon = det["init"].max() + pd.Timedelta(hours=scfg["max_lead_hours"]) if len(det) else now
    _drop_stale(storms, point, now, touched, horizon)
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


def score_storm(storm: pd.Series, fc_all: pd.DataFrame, psteps: dict[str, pd.DataFrame], cfg: dict) -> pd.DataFrame:
    """Every archived run that started before the storm ended, totalled over the final window, vs observed, at every
    point in psteps (point_steps)."""
    return pd.concat([_score_point(storm, p, fc_all, o, cfg) for p, o in psteps.items()] or [pd.DataFrame()],
                     ignore_index=True)


def _score_point(storm: pd.Series, point: str, fc_all: pd.DataFrame, osteps: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    s, e = storm.final_start, storm.final_end
    obs_mm = window_total(osteps, s, e)
    thr = cfg["verify"]["event_swe_in"] * MM_PER_IN
    fc = fc_all[(fc_all["point"] == point) & (fc_all["init"] < e)]
    rows = []
    keys = ["model", "init"] + (["member"] if "member" in fc.columns else [])
    for key, g in fc.groupby(keys, dropna=False):
        if g["valid_end"].max() < e or g["valid_start"].min() > s:
            continue  # this run doesn't cover the whole window (too short, or started mid-storm)
        model, init = key[0], key[1]
        member = key[2] if len(key) > 2 else np.nan
        water = window_total(g, s, e) / MM_PER_IN
        snow = window_total(g, s, e, "snow_in") if "snow_in" in g.columns and g["snow_in"].notna().any() else np.nan
        rows.append({"storm_id": storm.storm_id, "point": point, "model": model, "init": init, "member": member,
                     "lead_to_start_hr": (s - init) / pd.Timedelta(hours=1), "fc_water_in": water,
                     "fc_snow_in": snow, "obs_water_in": obs_mm / MM_PER_IN,
                     "fc_event": water * MM_PER_IN >= thr, "obs_event": obs_mm >= thr,
                     "fc_flow": run_flow(g, storm.point, s, e, cfg), "fc_modes": run_modes(g, storm.point, s, e, cfg),
                     "obs_flow": storm.get("obs_flow", "")})
    return pd.DataFrame(rows)


def _block_regimes(storm: pd.Series, blks, fc_all: pd.DataFrame, psteps: dict, rap, soundings, cfg: dict) -> list:
    """[(forecast regime, observed regime)] per block, the same for every model and point: the storm's defining run
    (its own flow, weighted by its precip) and the observed flow (regimes.observed_regime); '' where unknown."""
    g = fc_all[(fc_all["model"] == storm.get("defining_model")) & (fc_all["init"] == storm.get("defining_init"))
               & (fc_all["point"] == storm.point)]
    g = regimes.with_bands(g, cfg["regimes"]) if len(g) else g
    out = []
    for bs, be, _ in blks:
        steps = g[(g["valid_end"] > bs) & (g["valid_start"] < be)] if len(g) else g
        steps = steps.assign(w=steps["qpf_mm"] if steps["qpf_mm"].sum() > 0 else 1.0) if len(steps) else steps
        fc = regimes.dominant(regimes.phase_string(regimes.phases(steps, cfg["regimes"], "w"))) if len(steps) else ""
        obs = regimes.observed_regime(bs, be, storm.point, rap, soundings, psteps.get(storm.point), cfg)
        out.append((fc, regimes.dominant(obs["flow"])))
    return out


def score_blocks(storm: pd.Series, fc_all: pd.DataFrame, psteps: dict[str, pd.DataFrame], cfg: dict,
                 rap: pd.DataFrame | None = None, soundings: pd.DataFrame | None = None) -> pd.DataFrame:
    """The verified storm in 12 h blocks at every point in psteps: each deterministic run that covers a whole block and
    was issued before it began (within storms.max_lead_hours), its water and snow there vs the observed water, with
    the block's forecast and observed regime (_block_regimes)."""
    scfg, thr = cfg["storms"], cfg["verify"]["event_swe_in"]
    blks = blocks.storm_blocks(storm.final_start, storm.final_end, scfg["block_hours"], scfg["block_end_utc_hour"])
    det = fc_all[fc_all["member"].isna()] if "member" in fc_all.columns else fc_all
    regs = _block_regimes(storm, blks, det, psteps, rap, soundings, cfg)
    rows = []
    for point, osteps in psteps.items():
        fp = det[det["point"] == point]
        for (bs, be, _), (fc_regime, obs_regime) in zip(blks, regs):
            obs_in = window_total(osteps, bs, be) / MM_PER_IN
            runs = fp[(fp["init"] <= bs) & (fp["init"] >= bs - pd.Timedelta(hours=scfg["max_lead_hours"]))]
            for (model, init), g in runs.groupby(["model", "init"]):
                if g["valid_start"].min() > bs or g["valid_end"].max() < be:
                    continue                             # the run doesn't cover the whole block
                water = window_total(g, bs, be) / MM_PER_IN
                rows.append({"storm_id": storm.storm_id, "point": point, "block_start": bs, "block_end": be,
                             "model": model, "init": init, "lead_to_block_hr": (bs - init) / pd.Timedelta(hours=1),
                             "fc_water_in": water, "fc_snow_in": window_total(g, bs, be, "snow_in")
                             if "snow_in" in g.columns else np.nan, "obs_water_in": obs_in,
                             "fc_event": water >= thr, "obs_event": obs_in >= thr,
                             "fc_regime": fc_regime, "obs_regime": obs_regime})
    return pd.DataFrame(rows)


def load_block_scores(root=None) -> pd.DataFrame:
    p = (root or ARCHIVE_DIR) / "block_scores.csv"
    return pd.read_csv(p, parse_dates=["init", "block_start", "block_end"]) if p.exists() else pd.DataFrame()


def append_block_scores(scores: pd.DataFrame, root=None) -> None:
    """Add a storm's block scores; a storm scored again replaces its rows."""
    if scores.empty:
        return
    p = (root or ARCHIVE_DIR) / "block_scores.csv"
    if p.exists():
        old = pd.read_csv(p)
        scores = pd.concat([old[~old["storm_id"].astype(str).isin(scores["storm_id"].astype(str))], scores],
                           ignore_index=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    scores.to_csv(p, index=False)


def storm_span(storm: pd.Series) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The storm's window: the observed (final) one once verified, else the forecast one."""
    s = storm.final_start if pd.notna(storm.final_start) else storm.fc_start
    e = storm.final_end if pd.notna(storm.final_end) else storm.fc_end
    return s, e


def manual_totals(storm: pd.Series, totals: pd.DataFrame, cfg: dict, point: str | None = None) -> dict | None:
    """Hand-entered totals (data/entries/storm_totals.csv) for this storm: rows at `point` (default: the storm's
    point) overlapping its window,
    give or take storms.manual_match_hours (hand-entered times are approximate). Several rows (e.g. daily totals) are
    summed. {"new_snow_in", "water_in", "sources"}, or None if nothing was entered."""
    if totals is None or totals.empty:
        return None
    s, e = storm_span(storm)
    slack = pd.Timedelta(hours=cfg["storms"]["manual_match_hours"])
    m = totals[(totals["site"] == (point or storm.point)) & (totals["start_utc"] < e + slack)
               & (totals["end_utc"] > s - slack)]
    if m.empty:
        return None
    return {"new_snow_in": pd.to_numeric(m["new_snow_in"], errors="coerce").sum(min_count=1),
            "water_in": pd.to_numeric(m["water_in"], errors="coerce").sum(min_count=1),
            "sources": ", ".join(sorted(m["source"].unique()))}


def hand_obs_totals(storm: pd.Series, readings: pd.DataFrame, cfg: dict, source: str = "collins") -> dict | None:
    """Storm totals from hand-copied readings (archive.obs.load_hand_obs) inside the storm's window, give or take
    storms.manual_match_hours. Per variable, nothing is counted twice:
      - boards (12 h or longer) come first; where boards overlap (a 24 h board and the two 12 h boards inside it)
        the shorter ones are kept and the overlapping longer one is skipped;
      - a board missing that variable is filled from the hourly readings inside its period;
      - hourly readings outside every board's period (e.g. after the last board) are added.
    Max depth is reported as well. {"new_snow_in", "water_in", "max_depth_in", "sources"}, or None if there are no
    readings in the window."""
    if readings is None or readings.empty:
        return None
    s, e = storm_span(storm)
    slack = pd.Timedelta(hours=cfg["storms"]["manual_match_hours"])
    r = readings[(readings["start_utc"] >= s - slack) & (readings["end_utc"] <= e + slack)]
    if r.empty:
        return None

    boards = []                                          # non-overlapping boards, shortest periods first
    for _, b in r[r["period_h"] >= 12].sort_values(["period_h", "end_utc"]).iterrows():
        if not any(b.start_utc < k.end_utc and b.end_utc > k.start_utc for k in boards):
            boards.append(b)

    def total(col):
        hourly = r[(r["period_h"] < 12) & r[col].notna()]
        covered = pd.Series(False, index=hourly.index)
        amount, used = 0.0, set()
        for b in boards:
            inside = (hourly["start_utc"] >= b.start_utc) & (hourly["end_utc"] <= b.end_utc)
            covered |= inside
            if pd.notna(b[col]):
                amount, used = amount + float(b[col]), used | {"board"}
            elif inside.any():                           # board without this variable: its hours fill in
                amount, used = amount + float(hourly.loc[inside, col].sum()), used | {"hourly"}
        rest = hourly[~covered]
        if len(rest):
            amount, used = amount + float(rest[col].sum()), used | {"hourly"}
        return (amount if used else np.nan), used

    snow, how_s = total("new_snow_in")
    water, how_w = total("water_in")
    kinds = sorted(how_s | how_w)
    return {"new_snow_in": snow, "water_in": water, "max_depth_in": r["depth_in"].max(),
            "sources": f"{source} ({', '.join(kinds)})" if kinds else source}


def observed_snow(storm: pd.Series, totals: pd.DataFrame, readings: pd.DataFrame | None, cfg: dict,
                  point: str | None = None, obs_water_in: float | None = None) -> tuple[float, str]:
    """A verified storm's observed new snow at `point` (default: the storm's point) and where it came from: the owner's
    hand-entered storm total, else (the storm's point only: Alta) the hand-copied Collins readings, else 0 when that
    point's SNOTEL gauge saw no measurable water (obs_water_in; default the storm's). SNOTEL depth is too noisy for new
    snow, so a wet storm without hand entries has no observed snow yet: (nan, "")."""
    point = point or storm.point
    water = storm.get("obs_water_in") if obs_water_in is None else obs_water_in
    hand = [manual_totals(storm, totals, cfg, point)]
    if point == storm.point:
        hand.append(hand_obs_totals(storm, readings, cfg))
    for found in hand:
        if found and pd.notna(found["new_snow_in"]):
            return float(found["new_snow_in"]), found["sources"]
    if pd.notna(water) and water < cfg["verify"]["event_swe_in"]:
        return 0.0, "SNOTEL dry"
    return np.nan, ""


def bin_scores(storms: pd.DataFrame, scores: pd.DataFrame, totals: pd.DataFrame, readings: pd.DataFrame | None,
               cfg: dict) -> pd.DataFrame:
    """Every scored deterministic run (any lead) of every verified storm, at every scored point that has an observed
    snow total, with its bin score (cottonwoods.bins, event call included): storm_scores columns + obs_snow_in,
    obs_snow_source, fc_bin, obs_bin, bins_off, hit, bin_points, points, event, snow_err_in, water_err_in (forecast
    minus observed). Rebuilt each pass because hand totals arrive after a storm is verified."""
    if scores.empty or storms.empty:
        return pd.DataFrame()
    rows = []
    for _, st in storms[storms["status"] == "verified"].iterrows():
        sc = scores[(scores["storm_id"].astype(str) == str(st.storm_id)) & scores["fc_snow_in"].notna()]
        for point, g in sc.groupby("point"):
            obs_in, source = observed_snow(st, totals, readings, cfg, point, g["obs_water_in"].iloc[0])
            if np.isnan(obs_in):
                continue
            for _, r in g.iterrows():
                rows.append({**r.to_dict(), "obs_snow_in": obs_in, "obs_snow_source": source,
                             **bins.bin_score(r.fc_snow_in, obs_in, cfg["verify"], r.fc_event, r.obs_event),
                             "snow_err_in": r.fc_snow_in - obs_in, "water_err_in": r.fc_water_in - r.obs_water_in})
    return pd.DataFrame(rows)


def regime_bin_scores(storms: pd.DataFrame, fc: pd.DataFrame, factors: pd.DataFrame, psteps: dict[str, pd.DataFrame],
                      totals: pd.DataFrame, readings: pd.DataFrame | None, cfg: dict) -> pd.DataFrame:
    """LOCAL ONLY (owner 2026-10-29; scripts/regime_scores.py, never the cloud pass): every verified storm whose runs
    are loaded, scored twice by the pass's own rules (score_storm + bin_scores): plain Kuchera snow and regime-adjusted
    snow (regimes.apply_factors with `factors`, each step by its regime's factor). bin_scores rows plus snow = "plain"
    or "regime". Uses today's factors, so refitting them changes these scores (they test the current factors)."""
    if fc.empty or storms.empty:
        return pd.DataFrame()
    v = storms[(storms["status"] == "verified") & (pd.to_datetime(storms["final_start"]) >= fc["init"].min())]
    if v.empty:
        return pd.DataFrame()
    adj = regimes.apply_factors(fc, factors, cfg["regimes"])
    adj["snow_in"] = adj["snow_in_adj"] if "snow_in_adj" in adj else adj.get("snow_in")
    out = []
    for label, f in (("plain", fc), ("regime", adj)):
        sc = pd.concat([score_storm(st, f, psteps, cfg) for _, st in v.iterrows()], ignore_index=True)
        b = bin_scores(v, sc, totals, readings, cfg)
        if not b.empty:
            out.append(b.assign(snow=label))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def save_bin_scores(table: pd.DataFrame, root=None) -> None:
    p = (root or PROCESSED_DIR) / "bin_scores.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(p, index=False)


def load_scores(root=None) -> pd.DataFrame:
    p = (root or ARCHIVE_DIR) / "storm_scores.csv"
    return pd.read_csv(p, parse_dates=["init"]) if p.exists() else pd.DataFrame()


def append_scores(scores: pd.DataFrame, root=None) -> None:
    """Add a storm's scores; rows for a (storm, point) already scored are replaced (re-scoring)."""
    p = (root or ARCHIVE_DIR) / "storm_scores.csv"
    if scores.empty:
        return
    if p.exists():
        old = pd.read_csv(p)
        done = set(zip(scores["storm_id"].astype(str), scores["point"]))
        old = old[[k not in done for k in zip(old["storm_id"].astype(str), old["point"])]]
        scores = pd.concat([old, scores], ignore_index=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    scores.to_csv(p, index=False)


def _set_observed_flow(storms: pd.DataFrame, i, osteps: pd.DataFrame, rap, soundings, cfg: dict) -> None:
    st = storms.loc[i]
    o = regimes.observed_regime(st.final_start, st.final_end, st.point, rap, soundings, osteps, cfg)
    storms.at[i, "obs_flow"], storms.at[i, "obs_flow_source"], storms.at[i, "obs_flow_balloon"] = \
        o["flow"], o["source"], o["balloons"]


def _backfill_verified(storms: pd.DataFrame, fc: pd.DataFrame, psteps: dict, osteps: pd.DataFrame, rap, soundings,
                       cfg: dict, root=None) -> None:
    """Bring storms verified before a scoring change up to date, while their runs are still loaded (2026-10-29):
    the RAP observed flow where they only had the balloons (and the forecast flow again from the defining run, whose
    winds are now earth-relative), scores at points not scored yet, and block scores (with block regimes) where they
    have none or were made before RAP covered them. Existing storm-score rows stay."""
    scores, blocked = load_scores(root), load_block_scores(root)
    if scores.empty or fc.empty:
        return
    have = set(zip(scores["storm_id"].astype(str), scores["point"]))
    for i, st in storms[storms["status"] == "verified"].iterrows():
        if pd.isna(st.final_start) or st.final_start < fc["init"].min():
            continue                                     # its runs are no longer loaded
        relabel = st.get("obs_flow_source") != "rap"
        if relabel:
            _set_observed_flow(storms, i, osteps, rap, soundings, cfg)
            run = fc[(fc["model"] == st.defining_model) & (fc["init"] == st.defining_init)]
            if len(run):
                storms.at[i, "fc_flow"] = run_flow(run, st.point, st.fc_start, st.fc_end, cfg)
        new = {p: o for p, o in psteps.items() if (str(st.storm_id), p) not in have}
        if new:
            append_scores(score_storm(storms.loc[i], fc, new, cfg), root)
        mine = blocked[blocked["storm_id"].astype(str) == str(st.storm_id)] if not blocked.empty else blocked
        if mine.empty or "obs_regime" not in mine or (relabel and storms.at[i, "obs_flow_source"] == "rap"):
            append_block_scores(score_blocks(storms.loc[i], fc, psteps, cfg, rap, soundings), root)


def run(fc: pd.DataFrame, obs: pd.DataFrame, cfg: dict, now: pd.Timestamp, root=None,
        soundings: pd.DataFrame | None = None, rap: pd.DataFrame | None = None,
        fc_raw: pd.DataFrame | None = None) -> pd.DataFrame:
    """One pass: update forecast storms from the newest runs, verify any that are over. Returns the table.

    soundings: balloon summaries (cottonwoods.obs.sounding.summarize); rap: hourly RAP analyses
    (archive.obs.load_rap). Together they give the observed regime phases (regimes.observed_regime).
    fc_raw: blended models' unmodified runs (load_forecasts raw=True, "GFS raw"): scored next to fc, never used to
    find or time storms.
    """
    storms = update_forecast_storms(load_storms(root), fc, cfg, now)
    fc = pd.concat([fc, fc_raw], ignore_index=True) if fc_raw is not None and not fc_raw.empty else fc
    psteps = point_steps(obs, cfg)
    osteps = psteps.get(cfg["storms"]["point"], pd.DataFrame(columns=["valid_start", "valid_end", "qpf_mm"]))
    for i, st in storms[storms["status"] == "forecast"].iterrows():
        done = ready_to_verify(st, osteps, cfg, now)
        if done is None:
            continue
        storms.loc[i, ["obs_start", "obs_end"]] = [done["obs_start"], done["obs_end"]]
        storms.at[i, "final_start"] = min(st.fc_start, done["obs_start"]) if pd.notna(done["obs_start"]) else st.fc_start
        storms.at[i, "final_end"] = max(st.fc_end, done["obs_end"]) if pd.notna(done["obs_end"]) else st.fc_end
        storms.at[i, "obs_water_in"] = window_total(osteps, storms.at[i, "final_start"], storms.at[i, "final_end"]) / MM_PER_IN
        _set_observed_flow(storms, i, osteps, rap, soundings, cfg)
        storms.at[i, "status"] = "verified"
        storms.at[i, "updated_at"] = now
        append_scores(score_storm(storms.loc[i], fc, psteps, cfg), root)
        append_block_scores(score_blocks(storms.loc[i], fc, psteps, cfg, rap, soundings), root)
    _backfill_verified(storms, fc, psteps, osteps, rap, soundings, cfg, root)
    write_df(storms[COLS], storms_path(root))
    return storms
