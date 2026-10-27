"""Times for people: every report shows Mountain time, 24-hour, with MDT/MST chosen by the date.

All data stays in UTC (model runs and observations are UTC). Only what's displayed is converted, using the
America/Denver time-zone rules, so daylight saving is handled automatically: UTC-6 (MDT) in summer and early fall,
UTC-7 (MST) from the first Sunday of November. 04:49 UTC on 10/27/2026 shows as "2249 MDT" on 10/26.
Model runs keep their cycle name, which is how models are known everywhere, with the local time added:
"10/26 18z (1200 MDT)".

(SNOTEL's own timestamps are local STANDARD time all year, with no DST; that's handled where SNOTEL is read,
not here.)
"""
from __future__ import annotations

import pandas as pd

LOCAL_TZ = "America/Denver"


def local(ts) -> pd.Timestamp:
    """A UTC time (naive or aware) as Mountain time."""
    t = pd.Timestamp(ts)
    return (t.tz_localize("UTC") if t.tzinfo is None else t).tz_convert(LOCAL_TZ)


def clock(ts, weekday: bool = False, date: bool = True) -> str:
    """'Sat 10/31 0300 MST' (weekday optional), '10/31 0300 MST', or '0300 MST'; '-' for a missing time."""
    if ts is None or pd.isna(ts):
        return "-"
    t = local(ts)
    fmt = ("%a " if weekday else "") + ("%m/%d " if date else "") + "%H%M %Z"
    return t.strftime(fmt)


def run(init) -> str:
    """A model run: '10/26 18z (1200 MDT)'."""
    if init is None or pd.isna(init):
        return "-"
    t = pd.Timestamp(init)
    t = t.tz_convert("UTC").tz_localize(None) if t.tzinfo is not None else t
    return f"{t:%m/%d %H}z ({clock(t, date=False)})"


def now_line(now) -> str:
    """'made Mon 10/26 2249 MDT (0449 UTC)'."""
    t = pd.Timestamp(now)
    t = t.tz_convert("UTC").tz_localize(None) if t.tzinfo is not None else t
    return f"made {clock(t, weekday=True)} ({t:%H%M} UTC)"
