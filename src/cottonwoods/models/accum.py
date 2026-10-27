"""Precipitation accumulation handling.

Models store APCP as accumulations over windows that differ by model and forecast hour
(GFS: 0-3, 0-6, 6-9, 6-12, ...; HRRR: hourly). This module turns those windows into
per-step amounts, which is what a per-step SLR calculation needs.
"""
from __future__ import annotations

import math
import re

# Regular expression for text like "0-6 hour acc": two numbers (\d+) captured in ( ), a dash, "hour acc".
# \s* allows any spaces. Compiled once here so it isn't rebuilt on every call.
_ACC = re.compile(r"(\d+)\s*-\s*(\d+)\s*hour\s*acc", re.IGNORECASE)


def parse_acc_window(text: str) -> tuple[int, int] | None:
    """'0-6 hour acc fcst' -> (0, 6). Returns None if no accumulation window is present."""
    m = _ACC.search(str(text))
    return (int(m.group(1)), int(m.group(2))) if m else None


def gfs_fallback_window(fxx: int) -> tuple[int, int]:
    """GFS APCP bucket rule when the inventory can't be parsed: 6 h buckets that reset every 6 h.

    f003 -> (0,3), f006 -> (0,6), f009 -> (6,9), f012 -> (6,12). Verify against a real inventory.
    """
    # // is integer division (rounds down). For f009: (9-1)//6 = 1, so the bucket starts at 6*1 = hour 6.
    return (6 * ((fxx - 1) // 6), fxx)


def deaccumulate(records: list[tuple[int, int, float]]) -> list[tuple[int, int, float]]:
    """Convert accumulation-window records into per-step amounts.

    records: (a, b, v) meaning v accumulated from forecast hour a to b. Must start from hour 0.
    Returns (step_start_hr, step_end_hr, amount) between consecutive available outputs.
    A step whose bucket start was never resolved comes back NaN rather than being guessed.
    """
    # Idea: rebuild the running total since hour 0 at each output hour, then each step = this total minus the last.
    # Example (GFS): (0,3)=2mm, (0,6)=5mm, (6,9)=1mm -> totals 2, 5, 6 -> steps 2, 3, 1.
    recs = sorted(records, key=lambda r: (r[1], r[0]))  # by end hour; lambda is a tiny inline function
    cum: dict[int, float] = {0: 0.0}  # forecast hour -> total precip since hour 0
    out: list[tuple[int, int, float]] = []
    prev = 0
    for a, b, v in recs:
        if b in cum:
            continue  # duplicate output hour
        # The bucket started at hour a, so total at b = total at a + this bucket. If hour a is unknown
        # (a missing file), the total can't be rebuilt: mark it NaN instead of inventing a number.
        cum[b] = cum[a] + v if a in cum else math.nan
        step = cum[b] - cum[prev]
        # max(step, 0) removes tiny negative steps caused by GRIB rounding
        out.append((prev, b, max(step, 0.0) if not math.isnan(step) else math.nan))
        prev = b
    return out
