"""Probabilities from the archived runs, for the HTML storm report (owner, 2026-10-29). UNCALIBRATED: a share of
members is not yet a tested probability ("40%" means 40% of members, until verified storms show how often it
happens). Internal only, never in the feed.

  ensembles  GEFS, ECMWF ENS and RRFS ensemble members: the share whose storm water reaches each probs.water_in.
             RRFS members carry temperatures, so each has its own snow. GEFS / ENS are archived as water only, so each
             member's snow comes from its family's deterministic run (probs.families:
             GEFS -> GFS, ENS -> IFS): in every 12 h block, the member's water x that run's snow per inch of water
             there, which hands the members the family's snow ratio and rain/snow split. Then the share of members in
             each of the owner's snow bins (bins.py: the lowest bin containing the amount).
  lagged     a time-lagged ensemble of the high-resolution models (probs.lagged): each model's last `runs` runs that
             cover the whole storm are members; the same water and snow-bin shares. Spread between recent runs is the
             uncertainty signal at 0-48 h, where the global ensembles are too coarse for the canyons.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import bins, blocks
from .report import _snow_col, storm_runs
from .units import MM_PER_IN
from .windows import window_total, within_lead


def bin_shares(snow, vcfg: dict) -> dict[str, float]:
    """{bin label: share of members} over the owner's bins (lowest bin containing each amount); bins with members only."""
    snow = [float(x) for x in snow if pd.notna(x)]
    if not snow:
        return {}
    b = bins.snow_bins(vcfg, max(snow))
    idx = [bins.bin_index(x, b) for x in snow]
    return {bins.label(b[i]): idx.count(i) / len(idx) for i in sorted(set(idx))}


def water_shares(water, thresholds) -> dict[float, float]:
    """{threshold in: share of members with at least that much water}."""
    w = np.asarray([x for x in water if pd.notna(x)], float)
    return {t: float((w >= t).mean()) if len(w) else np.nan for t in thresholds}


def _covers(g: pd.DataFrame, s, e) -> bool:
    return not g.empty and g["valid_start"].min() <= s and g["valid_end"].max() >= e


def family_ratios(det: pd.DataFrame, blks, snow_col: str) -> list[float]:
    """A deterministic run's snow per inch of water in each block; where it has under 0.01" water in a block (no ratio
    to read) or doesn't reach it, its ratio over the whole stretch it covers; NaN if it has no water at all."""
    def ratio(s, e):
        w = window_total(det, s, e) / MM_PER_IN
        return window_total(det, s, e, snow_col) / w if w >= 0.01 else np.nan
    whole = ratio(max(det["valid_start"].min(), blks[0][0]), min(det["valid_end"].max(), blks[-1][1])) if len(det) else np.nan
    out = []
    for bs, be, _ in blks:
        r = ratio(bs, be) if _covers(det, bs, be) else np.nan
        out.append(r if pd.notna(r) else whole)
    return out


def ensemble_probs(st, fc: pd.DataFrame, cfg: dict, point: str) -> list[dict]:
    """Per ensemble (its newest run reaching the storm): model, init, members, full (covers the whole storm), water
    {threshold: share}, family (model, init) and snow_bins {bin: share} (empty without a family run)."""
    pc, s, e = cfg["probs"], st.fc_start, st.fc_end
    blks = blocks.storm_blocks(s, e, cfg["storms"]["block_hours"], cfg["storms"]["block_end_utc_hour"])
    runs = dict(storm_runs(fc[fc["point"] == point], cfg, s))
    out = []
    for model, g in runs.items():
        if "member" not in g.columns or not g["member"].notna().any():
            continue
        own = "snow_in" in g.columns and g["snow_in"].notna().any()      # members with their own snow (RRFS)
        fam = None if own else runs.get(pc["families"].get(model, ""))
        ratios = family_ratios(fam, blks, _snow_col(fam)) if fam is not None else []
        water, snow = [], []
        for _, m in g.groupby("member"):
            water.append(window_total(m, s, e) / MM_PER_IN)
            if own:
                snow.append(window_total(m, s, e, _snow_col(m)))
            elif ratios:
                snow.append(sum(window_total(m, bs, be) / MM_PER_IN * r for (bs, be, _), r in zip(blks, ratios)))
        out.append({"model": model, "init": g["init"].iloc[0], "members": len(water), "full": _covers(g, s, e),
                    "water": water_shares(water, pc["water_in"]), "own_snow": own,
                    "family": (pc["families"][model], fam["init"].iloc[0]) if fam is not None else None,
                    "snow_bins": bin_shares(snow, cfg["verify"])})
    return out


def lagged_probs(st, fc: pd.DataFrame, cfg: dict, point: str) -> dict:
    """The time-lagged high-resolution set: members = each probs.lagged model's last `runs` runs (within
    storms.max_lead_hours) that cover the whole storm. members, runs [(model, init)], short (runs that don't reach the
    whole storm, left out), water {threshold: share}, snow_bins {bin: share}."""
    lc, s, e = cfg["probs"]["lagged"], st.fc_start, st.fc_end
    fp = fc[(fc["point"] == point) & fc["model"].isin(lc["models"])]
    used, water, snow, short = [], [], [], 0
    for model, g in fp.groupby("model"):
        for init in sorted(g["init"].unique(), reverse=True)[:lc["runs"]]:
            r = within_lead(g[g["init"] == init], cfg["storms"]["max_lead_hours"])
            if not _covers(r, s, e):
                short += 1
                continue
            used.append((model, pd.Timestamp(init)))
            water.append(window_total(r, s, e) / MM_PER_IN)
            snow.append(window_total(r, s, e, _snow_col(r)))
    return {"members": len(used), "runs": used, "short": short,
            "water": water_shares(water, cfg["probs"]["water_in"]), "snow_bins": bin_shares(snow, cfg["verify"])}


def lagged_block_probs(st, fc: pd.DataFrame, cfg: dict, point: str) -> list[dict]:
    """The time-lagged high-resolution set block by block (12 h blocks, blocks.py), so short-range runs count in a
    storm longer than they reach: per block {start, end, label, members (runs covering the whole block), snow_share
    (share with at least probs.lagged.block_snow_in snow), median_snow}."""
    lc, s, e = cfg["probs"]["lagged"], st.fc_start, st.fc_end
    blks = blocks.storm_blocks(s, e, cfg["storms"]["block_hours"], cfg["storms"]["block_end_utc_hour"])
    fp = fc[(fc["point"] == point) & fc["model"].isin(lc["models"])]
    runs = [within_lead(g[g["init"] == init], cfg["storms"]["max_lead_hours"])
            for _, g in fp.groupby("model") for init in sorted(g["init"].unique(), reverse=True)[:lc["runs"]]]
    out = []
    for bs, be, lab in blks:
        snow = [window_total(r, bs, be, _snow_col(r)) for r in runs if _covers(r, bs, be) and r["init"].iloc[0] <= bs]
        out.append({"start": bs, "end": be, "label": blocks.label(lab), "members": len(snow),
                    "snow_share": float(np.mean([x >= lc["block_snow_in"] for x in snow])) if snow else np.nan,
                    "median_snow": float(np.median(snow)) if snow else np.nan})
    return out
