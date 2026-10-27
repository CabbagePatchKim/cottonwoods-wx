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


def choose_precip(lines: list[str], fxx: int, cumulative_only: bool = False) -> tuple[str, tuple[int, int]] | None:
    """Pick one precip message from inventory lines: the since-start total if present, else the latest bucket.

    Prefer the since-start total: with buckets, one missing forecast hour breaks the chain and every later step
    comes back unknown (a dropped connection at GFS f084 once cost 53 of 80 hours); with totals, a missing hour
    only merges its two neighboring steps.

    Returns (line, (start_hr, end_hr)). Lines without window text (ECMWF tp) are a total from hour 0.
    """
    found = []
    for line in lines:
        win = parse_acc_window(line)
        found.append((line, win if win else (0, fxx)))
    if not found:
        return None
    zero = [f for f in found if f[1][0] == 0 and f[1][1] == fxx]
    if zero or cumulative_only:
        return zero[0] if zero else None
    ends = [f for f in found if f[1][1] == fxx]
    return max(ends, key=lambda f: f[1][0]) if ends else None
