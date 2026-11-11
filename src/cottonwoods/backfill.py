"""The verification backfill (docs/PLAN.md step 4, owner 2026-11-10): past storms, found by the live pass's own rules,
with every observed total we have for them, so archived / re-pulled model runs can be scored on many storms.

Step 1, the storm list (this module, scripts/backfill_storms.py):
  - observed steps per winter (October-May, each handled alone so the gauge's summer gap and water-year reset never
    look like one huge hour) from the point's own SNOTEL gauges (config points.<p>.obs), or, before those existed,
    from config backfill.fallback_sites (Alta: Atwater only reports from the 2022-23 winter; before that Snowbird,
    9,170 ft, ~1.5 km from Collins);
  - storms = storms.detect_events with the live verification rule: hours >= storms.obs_wet_in, ended by
    storms.gap_hours dry, at least storms.min_total_in;
  - each storm's totals: the NWS statement (storms.pns_totals, the live rule) and the owner's journal stake reading
    (journal storms overlapping it).
Step 2, the runs (plan_runs, scripts/backfill_runs.py): for each storm and each model in config backfill.models,
the newest run of that model issued at least each of backfill.leads_hours before the storm began, fetched only up
to the storm's end (archive_run max_lead_hours) into its own archive (data/local/backfill/archive), never the live
one. Later steps score them with the live scoring code.
"""
from __future__ import annotations

import pandas as pd

from . import journal, storms
from .models.registry import REGISTRY, cycles
from .units import MM_PER_IN

WINTER = (10, 5)           # October through May

COLS = ["storm_id", "point", "final_start", "final_end", "hours", "obs_water_in", "gauge", "nws_snow_in",
        "nws_water_in", "nws_note", "journal_id", "journal_snow_in"]


def winters(obs: pd.DataFrame) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """[(Oct 1, Jun 1)] of every winter the observations touch (UTC, naive)."""
    if obs.empty:
        return []
    t = pd.to_datetime(obs["time_utc"])
    first = t.min().year - (1 if t.min().month < WINTER[0] else 0)
    last = t.max().year - (1 if t.max().month < WINTER[0] else 0)
    return [(pd.Timestamp(y, WINTER[0], 1), pd.Timestamp(y + 1, WINTER[1] + 1, 1)) for y in range(first, last + 1)]


def point_gauge_steps(obs: pd.DataFrame, cfg: dict, point: str) -> tuple[pd.DataFrame, str]:
    """One winter's observed steps at `point`: its own SNOTEL sites when they report, else the fallback sites.
    Returns (steps, the sites used)."""
    own = [s for s in (cfg["points"][point].get("obs") or []) if s in cfg["snotel"]]
    fallback = [s for s in (cfg.get("backfill") or {}).get("fallback_sites", {}).get(point, []) if s in cfg["snotel"]]
    for sites in (own, fallback):
        have = [s for s in sites if ((obs["site"] == s) & (obs["element"] == "PREC")).any()]
        if have:
            return storms.obs_steps(obs, have, cfg["gauge"]), "+".join(have)
    return pd.DataFrame(columns=["valid_start", "valid_end", "qpf_mm"]), ""


def observed_storms(obs: pd.DataFrame, cfg: dict, point: str) -> pd.DataFrame:
    """Every observed storm at `point` in the observations, winter by winter (see the module notes)."""
    scfg, rows = cfg["storms"], []
    t = pd.to_datetime(obs["time_utc"])
    for a, b in winters(obs):
        steps, gauge = point_gauge_steps(obs[(t >= a) & (t < b)], cfg, point)
        if steps.empty:
            continue
        ev = storms.detect_events(steps, scfg["obs_wet_in"] * MM_PER_IN, scfg["gap_hours"],
                                  scfg["min_total_in"] * MM_PER_IN)
        for _, e in ev.iterrows():
            rows.append({"storm_id": f"{e.start:%Y%m%d%H}", "point": point, "final_start": e.start,
                         "final_end": e.end, "hours": (e.end - e.start) / pd.Timedelta(hours=1),
                         "obs_water_in": e.total_mm / MM_PER_IN, "gauge": gauge})
    return pd.DataFrame(rows, columns=COLS[:7])


def attach_totals(st: pd.DataFrame, totals: pd.DataFrame, journal_storms: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """The storm list with each storm's NWS statement total (pns_totals) and journal stake reading added."""
    out = st.copy()
    for c in COLS[7:]:
        out[c] = pd.Series([None] * len(out), dtype=object)
    for i, s in out.iterrows():
        row = pd.Series({"point": s.point, "fc_start": s.final_start, "fc_end": s.final_end,
                         "final_start": s.final_start, "final_end": s.final_end})
        nws = storms.pns_totals(row, totals, cfg)
        if nws:
            out.loc[i, ["nws_snow_in", "nws_water_in", "nws_note"]] = [nws["new_snow_in"], nws["water_in"], nws["note"]]
        for _, j in journal_storms.iterrows():
            js, je = journal.storm_window(j)
            if js <= s.final_end and je >= s.final_start and s.point == "alta":
                out.at[i, "journal_id"], out.at[i, "journal_snow_in"] = j.storm_id, j.actual_in
                break
    return out


def plan_runs(found: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """The runs to fetch: one row per (model, init) with the storms it serves and the furthest lead any of them
    needs (hours to the latest storm end, capped at the model's own last hour). Storms under backfill.min_water_in
    are left out. A run that can't reach a storm's start (HRRR at 72 h) is left out for that storm."""
    b = cfg["backfill"]
    rows = []
    for _, s in found[found["obs_water_in"] >= b["min_water_in"]].iterrows():
        start, end = pd.Timestamp(s.final_start), pd.Timestamp(s.final_end)
        for model in b["models"]:
            hours = cycles(cfg, model)
            last = max(REGISTRY[model].fxx_list(start.to_pydatetime(), cfg))
            for lead in b["leads_hours"]:
                t = start - pd.Timedelta(hours=lead)
                earlier = [h for h in hours if h <= t.hour]          # the newest cycle at or before t
                init = (t.normalize() + pd.Timedelta(hours=max(earlier)) if earlier
                        else t.normalize() - pd.Timedelta(days=1) + pd.Timedelta(hours=max(hours)))
                need = (end - init) / pd.Timedelta(hours=1)
                if (start - init) / pd.Timedelta(hours=1) >= last:
                    continue                             # the run ends before the storm begins
                rows.append({"model": model, "init": init, "storm_id": str(s.storm_id), "lead_h": lead,
                             "max_lead_hours": min(need, last)})
    plan = pd.DataFrame(rows, columns=["model", "init", "storm_id", "lead_h", "max_lead_hours"])
    if plan.empty:
        return plan
    return (plan.groupby(["model", "init"], as_index=False)
            .agg(storms=("storm_id", lambda x: " ".join(sorted(set(x)))), max_lead_hours=("max_lead_hours", "max"))
            .sort_values(["model", "init"]).reset_index(drop=True))
