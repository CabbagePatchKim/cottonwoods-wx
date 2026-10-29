"""Side-by-side table of the latest run of every model, by day, for one point. The automated version of reading
Pivotal model by model: deterministic models give water and Kuchera snow per day; ensembles give "x of N
members" with measurable precip (the 7-10 day signal) and their mean water.

Days are 24 h windows ending at verify.window_end_utc_hour (12 UTC = 0600 MDT / 0500 MST), so "11/03" means
morning 11/02 to morning 11/03, the same window the morning snow report covers. Times shown are Mountain time.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import blocks, regimes, timefmt
from .config import PROCESSED_DIR
from .storms import bin_scores, hand_obs_totals, manual_totals, run_flow, run_modes
from .windows import window_total, within_lead
from .units import FT_PER_M, MM_PER_IN

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


def model_days(fc: pd.DataFrame, cfg: dict, point: str, days: int | None = None,
               start: pd.Timestamp | None = None) -> list[dict]:
    """The numbers behind model_table, one dict per model (newest run), in display order. Each day is a dict:
    deterministic {"water_in", "snow_in", "snow_in_regime"} (NaN where missing; snow_in_regime only exists if fc went
    through regime_adjusted), ensembles {"members_wet", "members", "mean_water_in"},
    or None where the run doesn't cover the whole day, or the day ends past the run's shown lead (config report:
    deterministic 168 h, ensembles 240 h for the 8-10 day tail). Used by the text report and the website feed."""
    rcfg = cfg["report"]
    fc = latest_runs(fc[fc["point"] == point])
    start = start or pd.Timestamp.now(tz="UTC").tz_localize(None)
    wins = day_windows(start, days or rcfg["days"], cfg["verify"]["window_end_utc_hour"])
    thr_mm = cfg["verify"]["event_swe_in"] * MM_PER_IN
    out = []
    for model, g in fc.groupby("model"):
        ens = "member" in g.columns and g["member"].notna().any()
        g = within_lead(g, rcfg["ensemble_lead_hours"] if ens else rcfg["deterministic_lead_hours"])
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


def model_rank(model: str) -> float:
    """Position in the display order (MODEL_ORDER); a scored unmodified run ("GFS raw") right after its model;
    unknown models go last."""
    from .archive.forecasts import RAW_LABEL
    base, raw = (model[:-len(RAW_LABEL)], 0.5) if model.endswith(RAW_LABEL) else (model, 0)
    return (MODEL_ORDER.index(base) if base in MODEL_ORDER else len(MODEL_ORDER)) + raw


def _by_model_order(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: model_rank(r["model"]))


def _cells(c: dict | None, regime: bool = False) -> tuple[str, str]:
    """One amount as two text numbers: water + snow, or wet members/N + mean water (ensembles); '-' where the run
    doesn't cover it. regime: show the regime-adjusted snow where there is one (the storm report)."""
    if c is None:
        return "-", ""
    if "members" in c:
        return f"{c['members_wet']}/{c['members']}", f"{c['mean_water_in']:.2f}"
    snow = c["snow_in_regime"] if regime and not np.isnan(c.get("snow_in_regime", np.nan)) else c["snow_in"]
    return f"{c['water_in']:.2f}", "" if np.isnan(snow) else f"{snow:.0f}"


def model_table(fc: pd.DataFrame, cfg: dict, point: str, days: int | None = None,
                start: pd.Timestamp | None = None) -> list[str]:
    """One point's daily table as text lines (newest run of each model). Every day is a fixed-width block under its
    date holding two numbers, named on a sub-header row: water + Kuchera snow for deterministic models, wet members/N
    + mean water for ensembles. Fixed widths, so each number sits under its own header."""
    rows = model_days(fc, cfg, point, days, start)
    if not rows:
        return ["(no archived runs)"]

    def line(model, run, cells):
        return (f"{model:<8}{run:<11}" + "".join(f"  {a:>5}{b:>6}" for a, b in cells)).rstrip()

    ends = [e for _, e in rows[0]["windows"]]
    out = [" " * 19 + "".join(f"{timefmt.local(e):%a %m/%d}".rjust(13) for e in ends)]
    for group, names in (([r for r in rows if not r["ensemble"]], ("water", "snow")),
                         ([r for r in rows if r["ensemble"]], ("wet/N", "mean"))):
        if group:
            out.append(line("model", "run", [names] * len(ends)))
            out += [line(r["model"], timefmt.cycle(r["init"]), [_cells(c) for c in r["days"]])
                    for r in group]
    return out


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


def render(fc: pd.DataFrame, cfg: dict, points: list[str], days: int | None = None,
           start: pd.Timestamp | None = None) -> str:
    """The text report: one table per point."""
    rcfg = cfg["report"]
    end_local = timefmt.clock(day_windows(start or pd.Timestamp.now(tz="UTC").tz_localize(None), 1,
                                          cfg["verify"]["window_end_utc_hour"])[0][1], date=False)
    lines = [f"Each day: the 24 h ending {end_local} on that date (the morning snow report's day). Amounts in inches.",
             "  water snow   deterministic models: water, and Kuchera snow at the point's elevation",
             f"  wet/N mean   ensembles: members with >= {cfg['verify']['event_swe_in']:.2f}\" water out of N, "
             "and their mean water",
             f"  run = model cycle (UTC). Deterministic shown to {rcfg['deterministic_lead_hours']} h after the run, "
             f"ensembles to {rcfg['ensemble_lead_hours']} h; '-' = not covered.", ""]
    for p in points:
        pt = cfg["points"][p]
        note = ", SNOTEL site, internal: not in the feed" if pt.get("internal") else ""
        lines += [f"=== {pt.get('label', p).upper()}  ({pt['elev_ft']:,} ft{note})", *model_table(fc, cfg, p, days, start), ""]
    return "\n".join(lines)


# --- the storm report (storms tracked by cottonwoods.storms) ---------------------------------------

def regime_adjusted(fc: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, bool]:
    """fc with snow_in_adj (each resort's history in each step's flow, regime_factors.csv) if factors exist."""
    factors = regimes.load_factors(PROCESSED_DIR / "regime_factors.csv")
    if factors.empty:
        return fc, False
    return regimes.apply_factors(fc, factors, cfg["regimes"]), True


def storm_runs(fc: pd.DataFrame, cfg: dict, s):
    """(model, steps) of every model's newest run that reaches a storm starting at s, within storms.max_lead_hours."""
    for model, g in fc.groupby("model"):
        g = within_lead(g[g["init"] == g["init"].max()], cfg["storms"]["max_lead_hours"])
        if g["valid_end"].max() > s:
            yield model, g


def _snow_col(g: pd.DataFrame) -> str:
    """The storm report's snow: regime-adjusted where there is one, like its totals table."""
    return "snow_in_adj" if "snow_in_adj" in g.columns and g["snow_in_adj"].notna().any() else "snow_in"


def _amount(v: float, cover: str) -> str:
    """A block's snow as text: '-' not reached, '.' dry (under 0.05"), '2.4' / '2.4*' (only part of the block)."""
    if cover == blocks.NONE or np.isnan(v):
        return "-"
    return ("." if v < 0.05 else f"{v:.1f}") + ("*" if cover == blocks.PART else "")


def storm_block_lines(st, fc: pd.DataFrame, cfg: dict, now=None, osteps: pd.DataFrame | None = None) -> list[str]:
    """A storm in fixed 12 h blocks (blocks.py), kept to two small tables so long storms stay readable:
      1. storms.block_point (Alta) snow by model, plus their median and the ensembles' share of wet members;
      2. every point's median snow (of the deterministic runs covering the whole block).
    Short-range runs fill the blocks they reach, so they count for a long storm too. Once the storm is under way
    (now past its start): an "observed" row with block_point's SNOTEL water in each block so far (osteps; "*" = the
    block still going) and a "rest" column, each model's snow from now to the storm's end ("*" = its run ends
    sooner): the rest of the storm, next to what has already fallen."""
    bcfg = cfg["storms"]
    blks = blocks.storm_blocks(st.fc_start, st.fc_end, bcfg["block_hours"], bcfg["block_end_utc_hour"])
    if len(blks) < 2:
        return []
    wet_mm = cfg["verify"]["event_swe_in"] * MM_PER_IN
    point, labels = bcfg["block_point"], {p: v.get("label", p) for p, v in cfg["points"].items()}
    head = [blocks.label(b[2]) for b in blks]
    det, ens, full = [], [], {p: [[] for _ in blks] for p in cfg["points"]}
    for model, g in sorted(storm_runs(fc, cfg, st.fc_start), key=lambda mg: model_rank(mg[0])):
        if "member" in g.columns and g["member"].notna().any():
            ens.append((model, g["init"].iloc[0], blocks.ensemble_blocks(g[g["point"] == point], blks, wet_mm)))
            continue
        for p in cfg["points"]:
            gp = g[g["point"] == p]
            got = blocks.run_blocks(gp, blks, _snow_col(gp)) if not gp.empty else [(np.nan, blocks.NONE)] * len(blks)
            for i, (v, cover) in enumerate(got):
                if cover == blocks.FULL and not np.isnan(v):
                    full[p][i].append(v)
            if p == point:
                det.append((model, g["init"].iloc[0], got))
    med = lambda vals: (float(np.median(vals)), blocks.FULL) if vals else (np.nan, blocks.NONE)  # noqa: E731
    w = max(8, max(len(h) for h in head) + 2)
    row = lambda name, run, cells: (f"{f'{name:<8}{run}':<19}" + "".join(f"{c:>{w}}" for c in cells)).rstrip()  # noqa: E731
    going = now is not None and st.fc_start < pd.Timestamp(now) < st.fc_end
    rest = {}
    if going:
        for model, g in storm_runs(fc, cfg, st.fc_start):
            gp = g[g["point"] == point]
            if gp.empty or ("member" in gp.columns and gp["member"].notna().any()):
                continue
            a = max(pd.Timestamp(now), gp["valid_start"].min())
            v = window_total(gp, a, st.fc_end, _snow_col(gp))
            rest[model] = f"{v:.1f}" + ("*" if gp["valid_end"].max() < st.fc_end else "")
    tail = lambda m: f"  {rest.get(m, '-'):>6}" if going else ""  # noqa: E731
    out = [f"    12 h blocks: {labels.get(point, point)} snow (in) by model; * = run covers only part of the block"
           + (f"; rest = snow from now ({timefmt.clock(now, date=False)}) to the end" if going else ""),
           row("model", "run", head) + (f"  {'rest':>6}" if going else "")]
    out += [row(m, timefmt.cycle(i), [_amount(v, c) for v, c in got]) + tail(m) for m, i, got in det]
    out.append(row("median", "", [_amount(*med(full[point][i])) for i in range(len(blks))]))
    if going and osteps is not None and not osteps.empty:
        seen = []
        for bs, be, _ in blks:
            if bs >= pd.Timestamp(now):
                seen.append("")
            else:
                water = window_total(osteps, bs, min(be, pd.Timestamp(now))) / MM_PER_IN
                seen.append(f"{water:.2f}" + ("*" if be > pd.Timestamp(now) else ""))
        out.append(row("observed", "water", seen).rstrip() + "  SNOTEL water (in) so far, not snow")
    out += [row(m, timefmt.cycle(i), ["-" if c == blocks.NONE else f"{v:.0%}" + ("*" if c == blocks.PART else "")
                                      for v, c in got]) + "  members wet" for m, i, got in ens]
    out += ["    12 h blocks: median snow (in) of the models covering the whole block, every point",
            row("point", "", head)]
    out += [row(_short(labels.get(p, p)), "", [_amount(*med(full[p][i])) for i in range(len(blks))])
            for p in cfg["points"]]
    return out


def storm_models(st, fc: pd.DataFrame, cfg: dict) -> list[dict]:
    """The numbers behind one storm's table: every model's newest run that reaches the storm, totalled over its
    window, in display order. Per point: deterministic {"water_in", "snow_in" (plain Kuchera), "snow_in_regime"
    (regime-adjusted, NaN if no factors)}, ensembles {"members_wet",
    "members", "mean_water_in"}, or None if the run was pulled before the point existed. snow_level_m is
    (first, last) over the wet steps at the storm's point, or None. Only steps within storms.max_lead_hours of the
    run's init count (a run that starts after the storm begins, or whose shown part ends before the storm does, is
    not "full"). Used by the text report and
    the website feed."""
    s, e = st.fc_start, st.fc_end
    thr = cfg["verify"]["event_swe_in"]
    out = []
    for model, g in storm_runs(fc, cfg, s):
        r = {"model": model, "init": g["init"].iloc[0], "valid_from": g["valid_start"].min(),
             "valid_to": g["valid_end"].max(), "ensemble": bool("member" in g.columns and g["member"].notna().any()),
             "late": bool(g["valid_start"].min() > s), "points": {}, "snow_level_m": None,
             "flow": run_flow(g, st.point, s, e, cfg), "modes": run_modes(g, st.point, s, e, cfg)}
        r["full"] = not r["late"] and bool(r["valid_to"] >= e)
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


def _storm_tail(r: dict) -> dict[str, str]:
    """A deterministic run's text columns after the amounts: snow level, flow and modes over the storm."""
    level = ""
    if r["snow_level_m"]:
        a, b = (m * FT_PER_M / 1000 for m in r["snow_level_m"])
        level = f"{a:.1f}k->{b:.1f}k ft"
    return {"snow level": level, "flow": r["flow"] or "", "modes": r["modes"] or ""}


def _short(label: str) -> str:
    """A point's column header in the storm table (14 wide): "Mill-D North SNOTEL" -> "Mill-D SNTL"."""
    words = label.replace("_snotel", " SNOTEL").split()
    return " ".join(words[:1] + ["SNTL"]) if words[-1] == "SNOTEL" else label


def _storm_table(models: list[dict], points: list[str], tail_w: dict[str, int],
                 labels: dict[str, str] | None = None) -> list[str]:
    """One storm's models as text lines, laid out like the daily table: a fixed-width block per point under its
    name holding two numbers (water + snow, or wet members/N + mean water) named on a sub-header row; then snow
    level, flow and modes at widths shared by every storm (tail_w), so all storms line up."""
    def line(model, run, covers, cells, tail):
        return (f"{model:<8}{run:<11}{covers:<19}" + "".join(f"   {a:>5}{b:>6}" for a, b in cells)
                + "".join(f"  {v:<{tail_w[k]}}" for k, v in tail.items())).rstrip()

    out = [" " * 38 + "".join(_short((labels or {}).get(p, p)).rjust(14) for p in points)]
    for group, names in (([r for r in models if not r["ensemble"]], ("water", "snow")),
                         ([r for r in models if r["ensemble"]], ("wet/N", "mean"))):
        if group:
            out.append(line("model", "run", "covers", [names] * len(points),
                            {k: k for k in tail_w} if not group[0]["ensemble"] else {}))
            out += [line(r["model"], timefmt.cycle(r["init"]),
                         "full storm" if r["full"] else f"from {timefmt.clock(r['valid_from'])}"
                         if r["late"] else f"to {timefmt.clock(r['valid_to'])}",
                         [("-", "") if r["late"] else _cells(r["points"].get(p), regime=True) for p in points],
                         {} if r["ensemble"] else _storm_tail(r)) for r in group]
    return out


def render_storms(storms: pd.DataFrame, fc: pd.DataFrame, cfg: dict, now: pd.Timestamp,
                  psteps: dict[str, pd.DataFrame] | None = None) -> str:
    """Each storm still ahead or under way, with every model's newest run totalled over the storm window.

    Deterministic: water (in) and Kuchera snow (in, regime-adjusted when available). Ensembles: members reaching the
    event threshold / N and mean water. A run that ends before the window does shows "to MM/DD HHMM" in its `covers`
    column (and its partial amounts); one that starts after the storm began shows "from MM/DD HHMM" and "-" (it
    can't see the whole storm, and a 0 would read like a forecast). A storm whose window has ended gets one
    "awaiting verification" line, like the feed leaving it out (owner, 2026-10-28). Times are Mountain.
    Under each storm's totals: the same runs in fixed 12 h blocks (storm_block_lines), where short-range runs count.
    """
    fc, adjusted = regime_adjusted(fc, cfg)
    lines = ["Snow is regime-adjusted per step (each resort's history in that step's flow, regime_factors.csv)."
             if adjusted else "Snow is NOT regime-adjusted (run scripts/regime_factors.py).",
             "  water snow   deterministic models: water, and snow at the point's elevation, inches",
             f"  wet/N mean   ensembles: members with >= {cfg['verify']['event_swe_in']:.2f}\" water out of N, "
             "and their mean water", "  run = model cycle (UTC); SNTL = internal SNOTEL-site point.", ""]
    open_ = storms[storms["status"] == "forecast"].sort_values("fc_start")
    ended = open_[open_["fc_end"] <= now]
    live = [(st, storm_models(st, fc, cfg)) for _, st in open_[open_["fc_end"] > now].iterrows()]
    tails = [_storm_tail(r) for _, models in live for r in models if not r["ensemble"]]
    tail_w = {k: max([len(k)] + [len(t[k]) for t in tails]) for k in ("snow level", "flow", "modes")}
    for st, models in live:
        s, e = st.fc_start, st.fc_end
        lines.append(f"=== Storm {st.storm_id}: {timefmt.clock(s, weekday=True)} - {timefmt.clock(e, weekday=True)} "
                     f"({(e - s) / pd.Timedelta(hours=1):.0f} h; window from {st.defining_model} "
                     f"{timefmt.run(st.defining_init)})")
        lines += [f"    {k}: {v}" for k, v in storm_notes(st)]
        labels = {p: v.get("label", p) for p, v in cfg["points"].items()}
        lines += _storm_table(models, list(cfg["points"]), tail_w, labels) if models else ["(no model reaches this storm)"]
        lines += storm_block_lines(st, fc, cfg, now, (psteps or {}).get(cfg["storms"]["block_point"])) if models else []
        lines.append("")
    for _, st in ended.iterrows():
        lines.append(f"--- Storm {st.storm_id} ({timefmt.clock(st.fc_start, weekday=True)} - "
                     f"{timefmt.clock(st.fc_end, weekday=True)}) ENDED: awaiting verification (about "
                     f"{cfg['storms']['gap_hours']} dry hours after it ends)")
    for _, st in recent_changes(storms).iterrows():
        lines.append(f"--- Storm {st.storm_id} ({timefmt.clock(st.fc_start, weekday=True)}) {st.status.upper()}: {st.note}")
    return "\n".join(lines) if lines else "No storms in the newest model runs.\n"


# --- recently verified storms: observed vs the last forecast before each one ------------------------------------------

def last_runs_before(scores: pd.DataFrame, storm_id: str, min_lead_hr: float = 0) -> pd.DataFrame:
    """Each model's newest scored run issued before the storm started (and at least min_lead_hr before it):
    deterministic water + snow; ensembles their mean water. Columns model, init, lead_hr, water_in, snow_in (NaN for
    ensembles)."""
    lead = scores["lead_to_start_hr"]
    sc = scores[(scores["storm_id"].astype(str) == str(storm_id)) & (lead > 0) & (lead >= min_lead_hr)]
    rows = []
    for model, g in sc.groupby("model"):
        g = g[g["init"] == g["init"].max()]
        ens = "member" in g.columns and g["member"].notna().any()
        rows.append({"model": model, "init": g["init"].iloc[0], "lead_hr": g["lead_to_start_hr"].iloc[0],
                     "water_in": g["fc_water_in"].mean(), "snow_in": np.nan if ens else g["fc_snow_in"].iloc[0]})
    return pd.DataFrame(rows)


def bin_picks(binned: pd.DataFrame, storm_id: str, cfg: dict) -> dict[str, pd.DataFrame]:
    """The bin-scored runs the report shows for one storm: each model's last run before it ("last") and its newest run
    at least each of verify.bin_score_leads_hours ahead ("24 h", ...). {kind: rows of `binned`, one per model}."""
    if binned.empty:
        return {}
    b = binned[binned["storm_id"].astype(str) == str(storm_id)]
    picks = {}
    for kind, lead in [("last", 0)] + [(f"{h} h", h) for h in cfg["verify"]["bin_score_leads_hours"]]:
        runs = last_runs_before(b, storm_id, lead) if not b.empty else pd.DataFrame()
        if not runs.empty:
            picks[kind] = runs.merge(b, on=["model", "init"])
    return picks


EVENT_MARK = {"false alarm": " FA", "miss": " miss"}


def _bin_cell(row) -> str:
    if row is None:
        return ""
    return f"{row.fc_bin} {row.points:+d}" + EVENT_MARK.get(getattr(row, "event", ""), "")


def recent_runs(table: pd.DataFrame, storm_id: str, cfg: dict) -> pd.DataFrame:
    """A storm's scored runs issued within verify.score_runs_hours before it (deterministic only)."""
    if table.empty:
        return table
    lead = table["lead_to_start_hr"]
    det = table["member"].isna() if "member" in table else True
    return table[(table["storm_id"].astype(str) == str(storm_id)) & det & (lead > 0)
                 & (lead <= cfg["verify"]["score_runs_hours"])]


def all_runs_table(binned: pd.DataFrame, scores: pd.DataFrame, storm_id: str, cfg: dict,
                   labels: dict[str, str], block_scores: pd.DataFrame | None = None) -> pd.DataFrame:
    """Every deterministic run within score_runs_hours of the storm, at every scored point: per model the runs
    counted, false alarms + misses, and per point the average bin score and average snow error (or, where that point
    has no observed snow yet, the average water error, "w"). Then its 12 h blocks (storms.score_blocks), runs within
    score_runs_hours of each block: blocks scored, average water error per block, false alarms + misses."""
    b, sc = recent_runs(binned, storm_id, cfg), recent_runs(scores, storm_id, cfg)
    if sc.empty:
        return pd.DataFrame()
    rows = []
    for model, g in sc.groupby("model"):
        bm = b[b["model"] == model] if not b.empty else b
        row = {"model": model, "runs": str(g["init"].nunique()),
               "FA/miss": str(int((g["fc_event"] != g["obs_event"]).sum()))}
        for point in sorted(sc["point"].unique(), key=list(cfg["points"]).index):
            bp, gp = (bm[bm["point"] == point] if not bm.empty else bm), g[g["point"] == point]
            if not bp.empty:
                row[_short(labels.get(point, point))] = f"{bp['points'].mean():+.1f} {bp['snow_err_in'].abs().mean():.1f}\""
            elif not gp.empty:
                row[_short(labels.get(point, point))] = f"w{(gp['fc_water_in'] - gp['obs_water_in']).abs().mean():.2f}\""
        rows.append(row)
    t = pd.DataFrame(rows)
    if block_scores is not None and not block_scores.empty:
        bl = block_scores[(block_scores["storm_id"].astype(str) == str(storm_id))
                          & (block_scores["lead_to_block_hr"] <= cfg["verify"]["score_runs_hours"])]
        bl = bl.assign(err=(bl["fc_water_in"] - bl["obs_water_in"]).abs(), bad=bl["fc_event"] != bl["obs_event"])
        agg = bl.groupby("model").agg(n=("err", "size"), err=("err", "mean"), bad=("bad", "sum"))
        t = t.merge(agg.reset_index(), on="model", how="outer")
        t["12h blocks"] = t["n"].map(lambda n: f"{n:.0f}" if pd.notna(n) else "")
        t["block water off"] = t["err"].map(lambda x: f"{x:.2f}\"" if pd.notna(x) else "")
        t["block FA/miss"] = t["bad"].map(lambda x: f"{x:.0f}" if pd.notna(x) else "")
        t = t.drop(columns=["n", "err", "bad"])
    return t.fillna("").sort_values("model", key=lambda c: c.map(model_rank))


def band_name(lo: int, hi: int) -> str:
    """A lead range's column name: '0-24 h', '1-3 d', '3-7 d'."""
    return f"{lo}-{hi} h" if hi <= 24 else f"{lo // 24}-{hi // 24} d"


def bin_totals(binned: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Season bin score per model over every verified storm with an observed snow total: per kind (last run, 24 h,
    ...) the summed points and right bins at the storm's point, e.g. "+3 (2 of 3 right)"; then per lead range
    (verify.lead_bands_hours: 0-24 h, 1-3 d, 3-7 d) the average over every deterministic run issued that far ahead, at
    every scored point, with the average snow error, so each model is compared with others forecasting from the same
    distance and long-range models get credit for early calls (owner, 2026-10-29)."""
    out = {}
    main = binned[binned["point"] == cfg["storms"]["point"]] if not binned.empty else binned
    for sid in binned["storm_id"].astype(str).unique() if not binned.empty else []:
        for kind, rows in bin_picks(main, sid, cfg).items():
            for _, r in rows.iterrows():
                t = out.setdefault(r.model, {}).setdefault(kind, [0, 0, 0])
                t[0], t[1], t[2] = t[0] + r.points, t[1] + bool(r.hit), t[2] + 1
    rows = [{"model": m, **{k: f"{p:+d} ({h} of {n} right)" for k, (p, h, n) in kinds.items()}}
            for m, kinds in out.items()]
    if not rows:
        return pd.DataFrame()
    t = pd.DataFrame(rows)
    det = binned[binned["member"].isna()] if "member" in binned else binned
    for lo, hi in cfg["verify"]["lead_bands_hours"]:
        band = det[(det["lead_to_start_hr"] > lo) & (det["lead_to_start_hr"] <= hi)]
        avg = band.groupby("model").agg(pts=("points", "mean"), err=("snow_err_in", lambda x: x.abs().mean()),
                                        n=("points", "size"))
        t[band_name(lo, hi)] = t["model"].map(lambda m: f"{avg.at[m, 'pts']:+.1f} {avg.at[m, 'err']:.1f}\" ({avg.at[m, 'n']})"
                                 if m in avg.index else "")
    return t.fillna("")


def regime_table(binned: pd.DataFrame, storms: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Season bin score per model by the storm's FORECAST regime (the dominant phase of its defining run: what is known
    ahead), every deterministic run within score_runs_hours at every scored point: "+1.8 (12)" = average score (runs x
    points). Each storm carries one regime for every model, so a model that called it dry is credited in that regime
    too (owner, 2026-10-29: the flow shouldn't come from each model's own run)."""
    if binned.empty or storms.empty or "fc_flow" not in storms:
        return pd.DataFrame()
    regime = storms.set_index(storms["storm_id"].astype(str))["fc_flow"].map(regimes.dominant)
    runs = pd.concat([recent_runs(binned, sid, cfg) for sid in binned["storm_id"].astype(str).unique()], ignore_index=True)
    if runs.empty:
        return pd.DataFrame()
    runs = runs.assign(regime=runs["storm_id"].astype(str).map(regime).replace("", "?"))
    avg = runs.groupby(["model", "regime"])["points"].agg(["mean", "size"])
    t = avg.apply(lambda r: f"{r['mean']:+.1f} ({r['size']:.0f})", axis=1).unstack("regime").fillna("").reset_index()
    return t.sort_values("model", key=lambda c: c.map(model_rank))


def block_regime_table(block_scores: pd.DataFrame | None, cfg: dict) -> pd.DataFrame:
    """Season 12 h block scores per model by the block's OBSERVED regime (RAP, weighted by observed precip), runs
    within score_runs_hours of each block: "0.04\" 1/12" = average water error, false alarms + misses / blocks."""
    if block_scores is None or block_scores.empty or "obs_regime" not in block_scores:
        return pd.DataFrame()
    bl = block_scores[block_scores["lead_to_block_hr"] <= cfg["verify"]["score_runs_hours"]]
    bl = bl.assign(err=(bl["fc_water_in"] - bl["obs_water_in"]).abs(), bad=bl["fc_event"] != bl["obs_event"],
                   regime=bl["obs_regime"].fillna("").replace("", "?"))
    agg = bl.groupby(["model", "regime"]).agg(err=("err", "mean"), bad=("bad", "sum"), n=("err", "size"))
    t = agg.apply(lambda r: f"{r['err']:.2f}\" {r['bad']:.0f}/{r['n']:.0f}", axis=1).unstack("regime").fillna("")
    return t.reset_index().sort_values("model", key=lambda c: c.map(model_rank))


def _flow_line(st) -> str:
    """'flow: forecast SW warm -1C 100% | observed SW warm 1C 100% (RAP) | balloons SW 50% -> W 50%' plus a flag when
    the balloons' dominant regime differs from the observed one."""
    obs = st.get("obs_flow") if isinstance(st.get("obs_flow"), str) else ""
    src = {"rap": "RAP, weighted by observed precip", "balloons": "SLC balloons"}.get(st.get("obs_flow_source"), "")
    fc = st.get("fc_flow") if isinstance(st.get("fc_flow"), str) else ""
    line = f"    flow: forecast {fc or '-'} | observed {obs or '-'}" + (f" ({src})" if src else "")
    balloon = st.get("obs_flow_balloon") if isinstance(st.get("obs_flow_balloon"), str) else ""
    if balloon and st.get("obs_flow_source") == "rap":
        line += f" | balloons {balloon}"
        if regimes.dominant(balloon) != regimes.dominant(obs):
            line += "  ** balloons disagree"
    return line


def render_verified(storms: pd.DataFrame, scores: pd.DataFrame, totals: pd.DataFrame, cfg: dict, now,
                    readings: pd.DataFrame | None = None, binned: pd.DataFrame | None = None,
                    block_scores: pd.DataFrame | None = None) -> str:
    """Storms verified in the last storms.report_verified_days: observed totals (SNOTEL water; hand-entered storm totals
    and hand-copied readings, e.g. Alta Collins snow and water) next to each model's last run before the storm, with
    the bin score (cottonwoods.bins) of that run and of its runs 24/72 h ahead, then the season's bin-score totals.
    binned: storms.bin_scores (computed here if not given)."""
    if binned is None:
        binned = bin_scores(storms, scores, totals, readings, cfg)
    days = cfg["storms"]["report_verified_days"]
    v = storms[(storms["status"] == "verified") & (pd.to_datetime(storms["final_end"]) >= pd.Timestamp(now) - pd.Timedelta(days=days))]
    if v.empty and binned.empty:
        return ""
    sc = cfg["verify"]["bin_score"]
    lines, tables = [f"=== Verified in the last {days} days (observed vs each model's last run before the storm)",
                     f"    bin score: right bin +{sc['reward']}, one bin off 0, each bin further -{sc['per_bin']}; "
                     f"FA = false alarm, miss = missed storm (>= {cfg['verify']['event_swe_in']:.2f}\" water): "
                     f"at most {sc['event_cap']:+d}"], []
    labels = {p: v.get("label", p) for p, v in cfg["points"].items()}
    if v.empty:                                          # a dry spell: the season's bin scores still show
        lines = [f"=== Verified in the last {days} days: none"]
    for _, st in v.sort_values("final_start").iterrows():
        lines.append(f"--- Storm {st.storm_id}: {timefmt.clock(st.final_start, weekday=True)} - "
                     f"{timefmt.clock(st.final_end, weekday=True)}")
        obs = f"SNOTEL {st.obs_water_in:.2f}\" water" if pd.notna(st.obs_water_in) else "SNOTEL -"
        found = [m for m in (manual_totals(st, totals, cfg), hand_obs_totals(st, readings, cfg)) if m]
        for man in found:
            parts = [f"{man['new_snow_in']:.0f}\" snow" if pd.notna(man["new_snow_in"]) else "",
                     f"{man['water_in']:.2f}\" water" if pd.notna(man["water_in"]) else ""]
            obs += f" | {man['sources']}: " + ", ".join(p for p in parts if p)
        if not found:
            obs += " | Collins: not entered yet (data/entries/)"
        lines.append(f"    observed: {obs}")
        lines.append(_flow_line(st))
        picks = bin_picks(binned[binned["point"] == st.point] if not binned.empty else binned, st.storm_id, cfg)
        if picks:
            first = next(iter(picks.values())).iloc[0]
            lines.append(f"    observed bin: {first.obs_bin} ({first.obs_snow_in:.0f}\", {first.obs_snow_source})")
        else:
            lines.append("    bin score: waiting for an observed snow total (hand entry)")
        main = scores[scores["point"] == st.point] if not scores.empty else scores
        runs = last_runs_before(main, st.storm_id) if not main.empty else pd.DataFrame()
        if runs.empty:
            lines.append("    (no scored runs)")
            continue
        t = pd.DataFrame({"model": runs["model"], "init": runs["init"].map(timefmt.run),
                          "lead": runs["lead_hr"].map(lambda h: f"{h:.0f} h before"),
                          "water": runs["water_in"].map(lambda w: f"{w:.2f}\""),
                          "snow": runs["snow_in"].map(lambda x: f"{x:.0f}\"" if pd.notna(x) else "")})
        for kind, rows in picks.items():
            by_model = {r.model: r for _, r in rows.iterrows()}
            t[f"bin {kind}" if kind == "last" else f"bin at {kind}"] = runs["model"].map(lambda m: _bin_cell(by_model.get(m)))
        tables.append(t.sort_values("model", key=lambda c: c.map(model_rank)))
        lines.append(("TABLE", len(tables) - 1))
        every = all_runs_table(binned, scores, st.storm_id, cfg, labels, block_scores)
        if not every.empty:
            lines.append(f"    every run up to {cfg['verify']['score_runs_hours']} h before, every gauged point: "
                         "average bin score and average snow error (w = water error, no observed snow there yet);"
                         " 12 h blocks: each run on every block it covered (water, all points)")
            tables.append(every)
            lines.append(("TABLE", len(tables) - 1))
    season = bin_totals(binned, cfg)
    if not season.empty:
        lines += ["", "=== Bin score, all verified storms with an observed snow total (score, right bins); by lead "
                  "range: average score, average snow error and (runs x points), every run issued that far ahead"]
        bands = [band_name(lo, hi) for lo, hi in cfg["verify"]["lead_bands_hours"]]
        season = season.rename(columns={k: f"bin {k}" if k == "last" else f"bin at {k}" for k in season.columns
                                        if k not in ["model", *bands]})
        tables.append(season.sort_values("model", key=lambda c: c.map(model_rank)))
        lines.append(("TABLE", len(tables) - 1))
    for title, table in (("=== By the storm's forecast regime (defining run's dominant flow): average bin score "
                          f"(runs x points), runs up to {cfg['verify']['score_runs_hours']} h before",
                          regime_table(binned, storms, cfg)),
                         ("=== 12 h blocks by observed regime (RAP): average water error, false alarms + misses / blocks",
                          block_regime_table(block_scores, cfg))):
        if not table.empty:
            lines += ["", title]
            tables.append(table)
            lines.append(("TABLE", len(tables) - 1))
    rendered = _aligned(tables)
    return "\n".join(rendered[x[1]] if isinstance(x, tuple) else x for x in lines) + "\n"
