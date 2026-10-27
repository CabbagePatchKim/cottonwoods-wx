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


def model_table(fc: pd.DataFrame, cfg: dict, point: str, days: int = 10, start: pd.Timestamp | None = None) -> pd.DataFrame:
    """One row per model (newest run): per-day water (in) and, where temperatures exist, Kuchera snow (in)."""
    fc = latest_runs(fc[fc["point"] == point])
    start = start or pd.Timestamp.now(tz="UTC").tz_localize(None)
    wins = day_windows(start, days, cfg["verify"]["window_end_utc_hour"])
    thr_mm = cfg["verify"]["event_swe_in"] * MM_PER_IN
    rows = []
    for model, g in fc.groupby("model"):
        row = {"model": model, "init": timefmt.run(g["init"].iloc[0])}
        ens = "member" in g.columns and g["member"].notna().any()
        for s, e in wins:
            label = f"{e:%m/%d}"
            if ens:
                per = g.groupby("member").apply(lambda m: window_total(m, s, e, strict=True), include_groups=False).dropna()
                row[label] = f"{int((per >= thr_mm).sum())}/{len(per)} {per.mean() / MM_PER_IN:.2f}" if len(per) else "-"
            else:
                water = window_total(g, s, e, strict=True) / MM_PER_IN
                snow = window_total(g, s, e, "snow_in", strict=True) if "snow_in" in g.columns else np.nan
                if np.isnan(water):
                    row[label] = "-"
                else:
                    row[label] = f"{water:.2f}" + (f" {snow:.0f}\"" if not np.isnan(snow) else "")
        rows.append(row)
    order = ["HRRR", "NAMNEST", "NAM", "RDPS", "GFS", "GDPS", "IFS", "GEFS", "ENS"]
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["_o"] = out["model"].map({m: i for i, m in enumerate(order)}).fillna(99)
    return out.sort_values("_o").drop(columns="_o").reset_index(drop=True)


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

def render_storms(storms: pd.DataFrame, fc: pd.DataFrame, cfg: dict) -> str:
    """Each storm still in the future/ongoing, with every model's newest run totalled over the storm window.

    Deterministic: water (in) and Kuchera snow (in). Ensembles: members reaching the event threshold / N and
    mean water. A run that ends before the window does is marked "(partial, to MM/DD HHMM)". Times are Mountain.
    """
    thr = cfg["verify"]["event_swe_in"]
    factors = regimes.load_factors(PROCESSED_DIR / "regime_factors.csv")
    adjusted = not factors.empty
    if adjusted:
        fc = regimes.apply_factors(fc, factors, cfg["regimes"])
    lines = ["Snow is regime-adjusted per step (each resort's history in that step's flow, regime_factors.csv)."
             if adjusted else "Snow is NOT regime-adjusted (run scripts/regime_factors.py).", ""]
    for _, st in storms[storms["status"] == "forecast"].sort_values("fc_start").iterrows():
        s, e = st.fc_start, st.fc_end
        lines.append(f"=== Storm {st.storm_id}: {timefmt.clock(s, weekday=True)} - {timefmt.clock(e, weekday=True)} "
                     f"({(e - s) / pd.Timedelta(hours=1):.0f} h; window from {st.defining_model} "
                     f"{timefmt.run(st.defining_init)})")
        if isinstance(st.outside_note, str) and st.outside_note:
            lines.append(f"    note: {st.outside_note}")
        if isinstance(st.fc_flow, str) and st.fc_flow:
            lines.append(f"    flow ({st.defining_model}): {st.fc_flow}")
        if isinstance(st.fc_modes, str) and st.fc_modes:
            lines.append(f"    modes ({st.defining_model}): {st.fc_modes}")
        if isinstance(st.fc_subperiods, str) and st.fc_subperiods:
            lines.append(f"    parts ({st.defining_model}): {st.fc_subperiods}")
        if isinstance(st.fc_periods, str) and st.fc_periods:
            lines.append(f"    models disagree: {st.fc_periods}")
        if isinstance(st.note, str) and st.note:
            lines.append(f"    note: {st.note}")
        rows = []
        for model, g in fc.groupby("model"):
            g = g[g["init"] == g["init"].max()]
            if g["valid_end"].max() <= s:
                continue  # newest run doesn't reach the storm
            partial = "" if g["valid_end"].max() >= e else f" (partial, to {timefmt.clock(g['valid_end'].max())})"
            row = {"model": model, "init": timefmt.run(g["init"].iloc[0])}
            for p in cfg["points"]:
                gp = g[g["point"] == p]
                if gp.empty:
                    row[p] = "-"  # this run was pulled before the point existed
                    continue
                if "member" in gp.columns and gp["member"].notna().any():
                    per = gp.groupby("member").apply(lambda m: window_total(m, s, e), include_groups=False) / MM_PER_IN
                    row[p] = f"{int((per >= thr).sum())}/{len(per)} mean {per.mean():.2f}\"{partial}"
                else:
                    water = window_total(gp, s, e) / MM_PER_IN
                    col = "snow_in_adj" if "snow_in_adj" in gp.columns and gp["snow_in_adj"].notna().any() else "snow_in"
                    snow = window_total(gp, s, e, col) if col in gp.columns else np.nan
                    row[p] = f"{water:.2f}\" {snow:.0f}\" snow{partial}" if not np.isnan(snow) else f"{water:.2f}\"{partial}"
            # Snow level at the storm's point while it's precipitating: first wet step -> last wet step, in ft
            gp = g[(g["point"] == st.point) & (g["valid_end"] > s) & (g["valid_start"] < e)]
            if "snow_level_m" in gp.columns:
                wet = gp[(gp["qpf_mm"] >= cfg["storms"]["wet_in"] * MM_PER_IN) & gp["snow_level_m"].notna()].sort_values("valid_end")
                if len(wet):
                    a, b = wet["snow_level_m"].iloc[0] * 3.281 / 1000, wet["snow_level_m"].iloc[-1] * 3.281 / 1000
                    row["snow level"] = f"{a:.1f}k->{b:.1f}k ft"
            flow = run_flow(g, st.point, s, e, cfg)
            if flow:
                row["flow"] = flow
            modes = run_modes(g, st.point, s, e, cfg)
            if modes:
                row["modes"] = modes
            rows.append(row)
        order = ["HRRR", "NAMNEST", "NAM", "RDPS", "GFS", "GDPS", "IFS", "GEFS", "ENS"]
        t = pd.DataFrame(rows)
        if not t.empty:
            t = t.assign(_o=t["model"].map({m: i for i, m in enumerate(order)}).fillna(99)).sort_values("_o").drop(columns="_o")
            t = t.fillna("")  # ensembles have no snow level or flow (precip only)
        lines.append(t.to_string(index=False) if not t.empty else "(no model reaches this storm)")
        lines.append("")
    # Storms dropped or merged in the last day, so a forecast that vanished is visible rather than silently gone
    recent = storms[storms["status"].isin(["dropped", "merged"])
                    & (pd.to_datetime(storms["updated_at"]) >= pd.Timestamp(storms["updated_at"].max()) - pd.Timedelta(days=1))]
    for _, st in recent.sort_values("fc_start").iterrows():
        lines.append(f"--- Storm {st.storm_id} ({timefmt.clock(st.fc_start, weekday=True)}) {st.status.upper()}: {st.note}")
    return "\n".join(lines) if lines else "No storms in the newest model runs.\n"
