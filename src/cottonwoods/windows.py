"""Totals over a time window from a run's steps (valid_start, valid_end, value): one rule, used by every report.

Two modes, chosen on purpose by the caller:
  strict=False  prorate steps that straddle the window edges and count a missing value as 0. For storm
                detection and storm totals: a single missing hour shouldn't make a whole storm disappear.
  strict=True   NaN unless the steps cover the whole window and none of the overlapping values is missing. For
                the daily model report: a day the run doesn't fully cover shows "-" rather than a low number.
Observed totals (gauges, SWE) have their own rules in verify.py and storms.obs_steps.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def window_total(steps: pd.DataFrame, s, e, col: str = "qpf_mm", strict: bool = False) -> float:
    """Sum of `col` over [s, e]; a step straddling an edge counts by the fraction of it inside the window."""
    if strict:
        inside = steps[(steps["valid_end"] > s) & (steps["valid_start"] < e)]
        if (inside.empty or inside["valid_start"].min() > s or steps["valid_end"].max() < e
                or inside["qpf_mm"].isna().any() or inside[col].isna().any()):
            return np.nan
    length = (steps["valid_end"] - steps["valid_start"]).dt.total_seconds()
    overlap = (steps["valid_end"].clip(upper=e) - steps["valid_start"].clip(lower=s)).dt.total_seconds().clip(lower=0)
    frac = (overlap / length.where(length > 0)).fillna(0)
    return float((steps[col].fillna(0) * frac).sum())
