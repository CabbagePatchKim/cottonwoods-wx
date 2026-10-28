"""Precip gauge running totals (SNOTEL PREC, Synoptic PREC) -> real hourly precip (config `gauge:`).

A gauge reports its running total in 0.1" steps and wobbles up and down by 0.1-0.2" with no precip at all. Summing
every hourly rise counts each wobble as precip (Atwater, dry 10/27/2026: net 0.0", summed rises 0.4"; whole months
30-300% too high). Instead (owner approved 2026-10-27):
  1. optionally smooth with a centered rolling median of median_hours (1 = off, the default: on the stored months
     the plain envelope matched each gauge's net change best, and a median loses precip at resets and at the newest
     hour);
  2. count only rises above the highest level reached so far (a running envelope), so a wobble down and back up
     counts nothing: noise counts at most once, up to the top of its band;
  3. a drop of more than reset_drop_in is the gauge being emptied or serviced: the envelope starts over there.
On the stored months this matches each gauge's net change within ~0.1".
"""
from __future__ import annotations

import pandas as pd


def gauge_increments(total: pd.Series, gcfg: dict) -> pd.Series:
    """Hourly precip (inches, >= 0) from a time-indexed running total; the first value's increment is 0."""
    s = total.dropna().sort_index()
    if s.empty:
        return s
    smooth = s.rolling(gcfg["median_hours"], center=True, min_periods=1).median()
    level, out = smooth.iloc[0], [0.0]
    for v in smooth.iloc[1:]:
        if v < level - gcfg["reset_drop_in"]:   # emptied: start over from the new level
            level = v
            out.append(0.0)
        elif v > level:
            out.append(v - level)
            level = v
        else:
            out.append(0.0)
    return pd.Series(out, index=s.index)
