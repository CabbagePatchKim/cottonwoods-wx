"""Side-by-side table of the latest run of every model, by day, for one point. The automated version of reading
Pivotal model by model: deterministic models give water and Kuchera snow per day; ensembles give "x of N
members" with measurable precip (the 7-10 day signal) and their mean water.

Days are 24 h windows ending at verify.window_end_utc_hour (12 UTC = 0600 MDT / 0500 MST), so "11/03" means
morning 11/02 to morning 11/03, the same window the morning snow report covers. Times shown are Mountain time.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import regimes, timefmt
from .config import PROCESSED_DIR
from .storms import run_flow, run_modes
from .windows import window_total

MM_PER_IN = 25.4
# Display order: short-range high-res first, then global, then ensembles
MODEL_ORDER = ["HRRR", "NAMNEST", "NAM", "RDPS", "GFS", "GDPS", "IFS", "GEFS", "ENS"]


def _aligned(tables: list[pd.DataFrame]) -> list[str]:
    """Render tables as left-aligned text with the SAME column widths across all of them (2 spaces apart), so every
    storm block in a report lines up the same way."""
    widths: dict[str, int] = {}
    for t in tables:
        for c in t.columns:
            widths[c] = max(widths.get(c, 0), len(str(c)), *(len(str(v)) for v in t[c]))
    out = []
    for t in tables:
        rows = [list(t.columns)] + t.astype(str).values.tolist()
        out.append("\n".join("  ".join(str(v).ljust(widths[c]) for c, v in zip(t.columns, r)).rstrip() for r in rows))
    return out


def day_windows(start: pd.Timestamp, days: int, end_hour: int) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """`days` consecutive 24 h windows, the first one ending at the next end_hour UTC after `start`."""
    e = start.normalize() + pd.Timedelta(hours=end_hour)
    if e <= start:
        e += pd.Timedelta(days=1)
    return [(e + pd.Timedelta(days=d - 1), e + pd.Timedelta(days=d)) for d in range(days)]


def latest_runs(fc: pd.DataFrame) -> pd.DataFrame:
    """Only the newest init of each model."""
    newest = fc.groupby("model")["init"].transform("max")
    return fc[fc["init"] == newest]


def model_days(fc: pd.DataFrame, cfg: dict, point: str, days: int = 10, start: pd.Timestamp | None = None) -> list[dict]:
    """The numbers behind model_table, one dict per model (newest run), in display order. Each day is a dict:
    deterministic {"water_in", "snow_in", "snow_in_regime"} (NaN where missing; snow_in_regime only exists if fc went
    through regime_adjusted), ensembles {"members_wet", "members", "mean_water_in"},
    or None where the run doesn't cover the whole day. Used by the text report and the website feed."""
    fc = latest_runs(fc[fc["point"] == point])
    start = start or pd.Timestamp.now(tz="UTC").tz_localize(None)
    wins = day_windows(start, days, cfg["verify"]["window_end_utc_hour"])
    thr_mm = cfg["verify"]["event_swe_in"] * MM_PER_IN
    out = []
    for model, g in fc.groupby("model"):
        ens = "member" in g.columns and g["member"].notna().any()
        cells = []
        for s, e in wins:
            if ens:
                per = g.groupby("member").apply(lambda m: window_total(m, s, e, strict=True), include_groups=False).dropna()
                cells.append({"members_wet": int((per >= thr_mm).sum()), "members": len(per),
                              "mean_water_in": per.mean() / MM_PER_IN} if len(per) else None)
            else:
                water = window_total(g, s, e, strict=True) / MM_PER_IN
                snow = window_total(g, s, e, "snow_in", strict=True) if "snow_in" in g.columns else np.nan
                adj = window_total(g, s, e, "snow_in_adj", strict=True) if "snow_in_adj" in g.columns else np.nan
                cells.append(None if np.isnan(water) else {"water_in": water, "snow_in": snow, "snow_in_regime": adj})
        out.append({"model": model, "init": g["init"].iloc[0], "ensemble": ens, "windows": wins, "days": cells})
    return _by_model_order(out)


def _by_model_order(rows: list[dict]) -> list[dict]:
    rank = {m: i for i, m in enumerate(MODEL_ORDER)}
    return sorted(rows, key=lambda r: rank.get(r["model"], 99))


def model_table(fc: pd.DataFrame, cfg: dict, point: str, days: int = 10, start: pd.Timestamp | None = None) -> pd.DataFrame:
    """One row per model (newest run): per-day water (in) and, where temperatures exist, Kuchera snow (in)."""
    rows = []
    for r in model_days(fc, cfg, point, days, start):
        row = {"model": r["model"], "init": timefmt.run(r["init"])}
        for (_, e), c in zip(r["windows"], r["days"]):
            if c is None:
                row[f"{e:%m/%d}"] = "-"
            elif r["ensemble"]:
                row[f"{e:%m/%d}"] = f"{c['members_wet']}/{c['members']} {c['mean_water_in']:.2f}"
            else:
                row[f"{e:%m/%d}"] = f"{c['water_in']:.2f}" + (f" {c['snow_in']:.0f}\"" if not np.isnan(c["snow_in"]) else "")
        rows.append(row)
    return pd.DataFrame(rows)


def staleness(cfg: dict, now, root=None, grace_hours: float = 4.0, lookback_cycles: int = 8) -> list[str]:
    """Warnings for models whose recent runs are missing from the archive.

    A run "should exist" once its posting delay (registry lag_hours) plus grace_hours has passed; the grace
    covers the 3-hour schedule and the pull itself. Checks the last `lookback_cycles` such runs of every model in
    config archive.models, by file name only, so it's instant. An empty list means everything is current.
    """
    from .archive.forecasts import run_path
    from .models.registry import recent_cycles

    out = []
    for model in cfg["archive"]["models"]:
        due = recent_cycles(model, cfg, pd.Timestamp(now).to_pydatetime() - pd.Timedelta(hours=grace_hours), lookback_cycles)
        missing = [t for t in due if not run_path(model, t, cfg, root).exists()]
        if not missing:
            continue
        have = [t for t in due if t not in missing]
        newest = f"newest archived {timefmt.run(max(have))}, {(pd.Timestamp(now) - pd.Timestamp(max(have))) / pd.Timedelta(hours=1):.0f} h old" \
            if have else "none of them archived"
        out.append(f"WARNING {model}: {len(missing)} of its last {len(due)} runs missing "
                   f"(latest missing {timefmt.run(max(missing))}); {newest}")
    return out


def render(fc: pd.DataFrame, cfg: dict, points: list[str], days: int = 10, start: pd.Timestamp | None = None) -> str:
    """The text report: one table per point."""
    lines = []
    for p in points:
        t = model_table(fc, cfg, p, days, start)
        end_local = timefmt.clock(day_windows(start or pd.Timestamp.now(tz="UTC").tz_localize(None), 1,
                                              cfg["verify"]["window_end_utc_hour"])[0][1], date=False)
        lines.append(f"=== {p.upper()}   (each day ends {end_local}; deterministic: water in + Kuchera snow; "
                     f"ensembles: members >= {cfg['verify']['event_swe_in']:.2f}\" / N + mean water)")
        lines.append(t.to_string(index=False) if not t.empty else "(no archived runs)")
        lines.append("")
    return "\n".join(lines)


# --- the storm report (storms tracked by cottonwoods.storms) ---------------------------------------

def regime_adjusted(fc: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, bool]:
    """fc with snow_in_adj (each resort's history in each step's flow, regime_factors.csv) if factors exist."""
    factors = regimes.load_factors(PROCESSED_DIR / "regime_factors.csv")
    if factors.empty:
        return fc, False
    return regimes.apply_factors(fc, factors, cfg["regimes"]), True


def storm_models(st, fc: pd.DataFrame, cfg: dict) -> list[dict]:
    """The numbers behind one storm's table: every model's newest run that reaches the storm, totalled over its
    window, in display order. Per point: deterministic {"water_in", "snow_in" (plain Kuchera), "snow_in_regime"
    (regime-adjusted, NaN if no factors)}, ensembles {"members_wet",
    "members", "mean_water_in"}, or None if the run was pulled before the point existed. snow_level_m is
    (first, last) over the wet steps at the storm's point, or None. Used by the text report and the website feed."""
    s, e = st.fc_start, st.fc_end
    thr = cfg["verify"]["event_swe_in"]
    out = []
    for model, g in fc.groupby("model"):
        g = g[g["init"] == g["init"].max()]
        if g["valid_end"].max() <= s:
            continue  # newest run doesn't reach the storm
        r = {"model": model, "init": g["init"].iloc[0], "valid_to": g["valid_end"].max(),
             "full": bool(g["valid_end"].max() >= e), "points": {}, "snow_level_m": None,
             "flow": run_flow(g, st.point, s, e, cfg), "modes": run_modes(g, st.point, s, e, cfg)}
        for p in cfg["points"]:
            gp = g[g["point"] == p]
            if gp.empty:
                r["points"][p] = None  # this run was pulled before the point existed
            elif "member" in gp.columns and gp["member"].notna().any():
                per = gp.groupby("member").apply(lambda m: window_total(m, s, e), include_groups=False) / MM_PER_IN
                r["points"][p] = {"members_wet": int((per >= thr).sum()), "members": len(per), "mean_water_in": per.mean()}
            else:
                has_adj = "snow_in_adj" in gp.columns and gp["snow_in_adj"].notna().any()
                r["points"][p] = {"water_in": window_total(gp, s, e) / MM_PER_IN,
                                  "snow_in": window_total(gp, s, e, "snow_in") if "snow_in" in gp.columns else np.nan,
                                  "snow_in_regime": window_total(gp, s, e, "snow_in_adj") if has_adj else np.nan}
        gp = g[(g["point"] == st.point) & (g["valid_end"] > s) & (g["valid_start"] < e)]
        if "snow_level_m" in gp.columns:
            wet = gp[(gp["qpf_mm"] >= cfg["storms"]["wet_in"] * MM_PER_IN) & gp["snow_level_m"].notna()].sort_values("valid_end")
            if len(wet):
                r["snow_level_m"] = (float(wet["snow_level_m"].iloc[0]), float(wet["snow_level_m"].iloc[-1]))
        out.append(r)
    return _by_model_order(out)


def storm_notes(st) -> list[tuple[str, str]]:
    """The labelled lines under a storm's heading, e.g. ("flow (IFS)", "SW warm 60% -> NW cold 40%"); empty ones left out."""
    m = st.defining_model
    pairs = [("note", st.outside_note), (f"flow ({m})", st.fc_flow), (f"modes ({m})", st.fc_modes),
             (f"parts ({m})", st.fc_subperiods), ("models disagree", st.fc_periods), ("note", st.note)]
    return [(k, v) for k, v in pairs if isinstance(v, str) and v]


def recent_changes(storms: pd.DataFrame) -> pd.DataFrame:
    """Storms dropped or merged in the last day, so a forecast that vanished is visible rather than silently gone."""
    recent = pd.to_datetime(storms["updated_at"]) >= pd.Timestamp(storms["updated_at"].max()) - pd.Timedelta(days=1)
    return storms[storms["status"].isin(["dropped", "merged"]) & recent].sort_values("fc_start")


def _point_text(c: dict | None) -> str:
    if c is None:
        return "-"
    if "members" in c:
        return f"{c['members_wet']}/{c['members']} mean {c['mean_water_in']:.2f}\""
    snow = c["snow_in_regime"] if not np.isnan(c["snow_in_regime"]) else c["snow_in"]
    return f"{c['water_in']:.2f}\" {snow:.0f}\" snow" if not np.isnan(snow) else f"{c['water_in']:.2f}\""


def render_storms(storms: pd.DataFrame, fc: pd.DataFrame, cfg: dict) -> str:
    """Each storm still in the future/ongoing, with every model's newest run totalled over the storm window.

    Deterministic: water (in) and Kuchera snow (in). Ensembles: members reaching the event threshold / N and
    mean water. A run that ends before the window does shows "to MM/DD HHMM" in its `covers` column. Times are Mountain.
    """
    fc, adjusted = regime_adjusted(fc, cfg)
    lines = ["Snow is regime-adjusted per step (each resort's history in that step's flow, regime_factors.csv)."
             if adjusted else "Snow is NOT regime-adjusted (run scripts/regime_factors.py).", ""]
    tables: list[pd.DataFrame] = []
    for _, st in storms[storms["status"] == "forecast"].sort_values("fc_start").iterrows():
        s, e = st.fc_start, st.fc_end
        lines.append(f"=== Storm {st.storm_id}: {timefmt.clock(s, weekday=True)} - {timefmt.clock(e, weekday=True)} "
                     f"({(e - s) / pd.Timedelta(hours=1):.0f} h; window from {st.defining_model} "
                     f"{timefmt.run(st.defining_init)})")
        lines += [f"    {k}: {v}" for k, v in storm_notes(st)]
        rows = []
        for r in storm_models(st, fc, cfg):
            row = {"model": r["model"], "init": timefmt.run(r["init"]),
                   "covers": "full storm" if r["full"] else f"to {timefmt.clock(r['valid_to'])}"}
            row.update({p: _point_text(c) for p, c in r["points"].items()})
            if r["snow_level_m"]:
                a, b = (m * 3.281 / 1000 for m in r["snow_level_m"])
                row["snow level"] = f"{a:.1f}k->{b:.1f}k ft"
            if r["flow"]:
                row["flow"] = r["flow"]
            if r["modes"]:
                row["modes"] = r["modes"]
            rows.append(row)
        # Same columns, same order, for every storm (blank where a model has no value, e.g. ensembles' snow level)
        cols = ["model", "init", "covers", *cfg["points"], "snow level", "flow", "modes"]
        t = pd.DataFrame(rows).reindex(columns=cols).fillna("")
        if t.empty:
            lines.append("(no model reaches this storm)")
        else:
            tables.append(t)
            lines.append(("TABLE", len(tables) - 1))  # filled in below, once all tables' widths are known
        lines.append("")
    for _, st in recent_changes(storms).iterrows():
        lines.append(f"--- Storm {st.storm_id} ({timefmt.clock(st.fc_start, weekday=True)}) {st.status.upper()}: {st.note}")
    rendered = _aligned(tables)
    lines = [rendered[x[1]] if isinstance(x, tuple) else x for x in lines]
    return "\n".join(lines) if lines else "No storms in the newest model runs.\n"
