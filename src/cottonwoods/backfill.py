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
Later steps score model runs against these storms with the live scoring code.
"""
from __future__ import annotations

import pandas as pd

from . import journal, storms
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
