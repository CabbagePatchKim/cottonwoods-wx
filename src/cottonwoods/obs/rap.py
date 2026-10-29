"""Hourly RAP analyses (13 km, NOAA): the OBSERVED 700 mb flow over the Cottonwoods (owner, 2026-10-29).

The SLC balloons give the flow only twice a day, ~25 km upstream. The RAP analysis (each hour's 0 h field: the
model's first guess corrected by that hour's observations; SPC's mesoanalysis maps are drawn from it) gives it every
hour at the points themselves, within hours, from NOAA's AWS bucket through Herbie (no key). RAP isn't one of the
scored models, so labelling storms with it favours none of them; the balloons stay as the independent cross-check.
Only 700 mb wind and temperature are read, a few small byte ranges per hour. Winds turned to true north (winds.py).
"""
from __future__ import annotations

import pandas as pd

from .. import winds
from ..models import fields as fl

SEARCH = r":(UGRD|VGRD|TMP):700 mb"


def fetch(hours: list[pd.Timestamp], points: dict[str, dict], cfg: dict, save_dir: str | None = None,
          workers: int = 4) -> pd.DataFrame:
    """One row per (hour, point): time_utc, point, u_700_ms, v_700_ms (earth-relative), t_700_k. Hours RAP doesn't
    have (not posted yet, or a missing file) are left out."""
    from herbie import Herbie  # lazy: offline tests don't need herbie

    kw = {"save_dir": save_dir} if save_dir else {}
    got: dict[pd.Timestamp, dict] = {}

    def download(t):
        return Herbie(t.to_pydatetime(), model="rap", product="awp130pgrb", fxx=0, verbose=False, **kw).download(
            SEARCH, verbose=False)

    def decode(t, path):
        got[t] = fl.point_fields(fl.open_grib(path), points)

    fl.parallel_hours(download, decode, list(hours), workers, label="rap analyses")
    rows = [{"time_utc": t, "point": p, **vals} for t, by in got.items() for p, vals in by.items()]
    if not rows:
        return pd.DataFrame()
    return winds.earth_winds(pd.DataFrame(rows).assign(model="RAP"), cfg).drop(columns=["model", "uv_earth"])
