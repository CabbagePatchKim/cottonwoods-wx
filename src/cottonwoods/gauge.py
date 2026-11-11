"""Precip gauge running totals (SNOTEL PREC, Synoptic PREC) -> real hourly precip (config `gauge:`).

A gauge reports its running total in 0.1" steps and wobbles up and down by 0.1-0.2" with no precip at all. Summing
every hourly rise counts each wobble as precip (Atwater, dry 10/27/2026: net 0.0", summed rises 0.4"; whole months
30-300% too high). Instead (owner approved 2026-10-27):
  1. optionally smooth with a centered rolling median of median_hours (1 = off, the default: on the stored months
     the plain envelope matched each gauge's net change best, and a median loses precip at resets and at the newest
     hour);
  2. count only rises above the highest level reached so far (a running envelope), so a wobble down and back up
     counts nothing: noise counts at most once, up to the top of its band;
  3. a drop of more than reset_drop_in is the gauge being emptied or serviced: the envelope starts over there;
  4. (2026-11-10, found by the verification backfill) a reading that leaves the level by more than reset_drop_in,
     up or down, and is back within reset_drop_in of it inside glitch_hours is a GLITCH, skipped: older SNOTEL
     hourly data has one-hour spikes (Atwater April 2023 and Snowbird March 2019: 34.5" -> 63.5" -> 34.5" every
     day at 08Z, PST midnight), which rules 2-3 counted as ~29" of precip a day;
  5. a rise above max_hourly_in per hour (hours since the previous reading, so a gap's rise is judged by its hours;
     2026-11-11: no 2016-2026 gap rose more than 0.4") is not precip (a service or sensor jump: Atwater 4/17/2023 went 34.6"
     -> 48.7" in 2 h with the snow pillow flat): the envelope moves up without counting it.
On the stored months this matches each gauge's net change within ~0.1"; rules 4-5 change nothing on clean months.
"""
from __future__ import annotations

import pandas as pd


def gauge_increments(total: pd.Series, gcfg: dict) -> pd.Series:
    """Hourly precip (inches, >= 0) from a time-indexed running total; the first value's increment is 0."""
    s = total.dropna().sort_index()
    if s.empty:
        return s
    smooth = s.rolling(gcfg["median_hours"], center=True, min_periods=1).median()
    vals, times = smooth.to_numpy(), smooth.index
    jump, back, cap = gcfg["reset_drop_in"], pd.Timedelta(hours=gcfg["glitch_hours"]), gcfg["max_hourly_in"]
    level, out = vals[0], [0.0]
    for i in range(1, len(vals)):
        v = vals[i]
        if abs(v - level) > jump and _returns(vals, times, i, level, jump, back):
            out.append(0.0)                      # a glitch: back to the level soon after
        elif v < level - jump:                   # emptied: start over from the new level
            level = v
            out.append(0.0)
        elif v > level:
            hours = max(1.0, (times[i] - times[i - 1]) / pd.Timedelta(hours=1))   # missing hours: the rise spans them
            out.append(v - level if v - level <= cap * hours else 0.0)   # a jump too big for its hours isn't precip
            level = v
        else:
            out.append(0.0)
    return pd.Series(out, index=s.index)


def _returns(vals, times, i, level, jump, within) -> bool:
    """True if a later reading within `within` of reading i is back within `jump` of `level`."""
    j = i + 1
    while j < len(vals) and times[j] - times[i] <= within:
        if abs(vals[j] - level) <= jump:
            return True
        j += 1
    return False
