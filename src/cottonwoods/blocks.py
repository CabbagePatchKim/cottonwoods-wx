"""A storm in fixed 12 h blocks (owner, 2026-10-29), so short-range models count for the part of a long storm they
reach instead of dropping out of it.

A storm's own parts come from one model's timing, so another model's snow can fall between them, and a model must
cover a whole part to get an amount. Blocks are the same for every model and run: edges at 00z and 12z (about
6 pm / 6 am Mountain; config storms.block_hours, block_end_utc_hour), clipped to the storm window, so the blocks of
a storm add up to its total. A run gets each block it reaches: "full" if it covers the whole block, "part" if only
some of it (the block its run ends in, or starts in when the run began mid-storm), "none" if it doesn't reach it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .windows import window_total

FULL, PART, NONE = "full", "part", "none"


def storm_blocks(s, e, hours: int = 12, end_utc_hour: int = 0) -> list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    """[(block start, block end, unclipped block start)] covering the storm [s, e], edges on the fixed grid."""
    s, e = pd.Timestamp(s), pd.Timestamp(e)
    step = pd.Timedelta(hours=hours)
    edge = s.normalize() + pd.Timedelta(hours=end_utc_hour % hours)
    while edge > s:
        edge -= step
    while edge + step <= s:
        edge += step
    out = []
    while edge < e:
        out.append((max(edge, s), min(edge + step, e), edge))
        edge += step
    return out


def run_blocks(steps: pd.DataFrame, blocks, col: str) -> list[tuple[float, str]]:
    """One run's steps at one point -> [(amount of `col` in the block, cover)] for each block."""
    if steps.empty:
        return [(np.nan, NONE)] * len(blocks)
    first, last = steps["valid_start"].min(), steps["valid_end"].max()
    out = []
    for bs, be, _ in blocks:
        cs, ce = max(bs, first), min(be, last)
        if ce <= cs:
            out.append((np.nan, NONE))
        else:
            out.append((window_total(steps, cs, ce, col), FULL if (cs, ce) == (bs, be) else PART))
    return out


def ensemble_blocks(steps: pd.DataFrame, blocks, wet_mm: float) -> list[tuple[float, str]]:
    """One ensemble run at one point -> [(share of members with >= wet_mm water in the block, cover)]."""
    if steps.empty:
        return [(np.nan, NONE)] * len(blocks)
    per = {m: run_blocks(g, blocks, "qpf_mm") for m, g in steps.groupby("member")}
    out = []
    for i in range(len(blocks)):
        vals = [v[i] for v in per.values()]
        cover = vals[0][1]
        out.append((float(np.mean([a >= wet_mm for a, _ in vals])) if cover != NONE else np.nan, cover))
    return out


def label(start: pd.Timestamp, tz: str = "America/Denver") -> str:
    """A block by its local day and half: 'Fri day' (about 6 am - 6 pm) or 'Fri ngt' (6 pm - 6 am, the night
    after that day). `start` is the unclipped block start (UTC)."""
    mid = (pd.Timestamp(start).tz_localize("UTC") + pd.Timedelta(hours=6)).tz_convert(tz)
    day = 6 <= mid.hour < 18
    return f"{(mid if day else mid - pd.Timedelta(hours=12)):%a} {'day' if day else 'ngt'}"
