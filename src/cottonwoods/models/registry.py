"""Which models we archive, when their cycles run, and how far out to pull.

To add a model: add a ModelSpec here and a puller returning the same columns as hrrr.pull_run
(model, init, lead_hr, valid_start, valid_end, point, grid_dist_km, qpf_mm, psfc_hpa, t_<lev>_k ...).
Ensembles return precip only, with an extra `member` column.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Callable


# A dataclass is a class that just holds named fields (Python writes __init__ for you).
# frozen=True makes instances read-only, so a spec can't be changed by accident.
@dataclass(frozen=True)
class ModelSpec:
    name: str
    # Callable[[args], result] = "a function taking these args and returning this". fxx_list is a function
    # (not a fixed list) because the answer depends on the config and, for some models, the init hour.
    fxx_list: Callable[[datetime, dict], list[int]]
    lag_hours: float                             # how long after init the run is typically fully posted
    members: int = 0                             # ensemble size (0 = deterministic model)
    only_cycles: tuple[int, ...] | None = None   # a model that runs at fewer hours than the shared cycles (GDPS)


def cycles(cfg: dict, model: str | None = None) -> list[int]:
    """Init hours (UTC) used for EVERY model, so runs line up across models: config archive.cycles_utc.

    A model that doesn't run at some of those hours (GDPS: 00/12z only) gets the hours it does run.
    """
    hours = sorted(cfg["archive"]["cycles_utc"])
    only = REGISTRY[model].only_cycles if model in REGISTRY else None
    return [h for h in hours if only is None or h in only]


def check_cycle(init: datetime, cfg: dict, model: str | None = None) -> None:
    """Refuse a run outside the shared cycle hours (e.g. an 03z HRRR, which only reaches 18 h)."""
    if init.hour not in cycles(cfg, model) or init.minute:
        raise ValueError(f"init {init:%Y-%m-%d %H:%M}z is not one of the shared cycles {cycles(cfg, model)} UTC "
                         f"for {model or 'this model'} (config archive.cycles_utc)")


def _steps(*parts: tuple[int, int, int]) -> list[int]:
    """_steps((3, 144, 3), (150, 360, 6)) -> 3, 6, ..., 144, 150, 156, ..., 360 (each part: first, last, step)."""
    return [h for first, last, step in parts for h in range(first, last + 1, step)]


def _hrrr_fxx(init: datetime, cfg):
    # range() stops BEFORE its end value: range(1, 49) is hours 1..48
    return list(range(1, cfg["archive"]["hrrr_max_fxx"] + 1))


def _gfs_fxx(init: datetime, cfg):
    a = cfg["archive"]
    return list(range(a["gfs_step_hours"], a["gfs_max_fxx"] + 1, a["gfs_step_hours"]))


def _ifs_fxx(init: datetime, cfg):
    # ECMWF HRES: 00/12z run to 360 h (3-hourly to 144, then 6-hourly); 06/18z stop at 144 h
    return _steps((3, 144, 3), (150, 360, 6)) if init.hour in (0, 12) else _steps((3, 144, 3))


def _ens_fxx(init: datetime, cfg):
    # ECMWF ENS precip is a running total, so only the hours that land on 00 and 12 UTC are needed: their
    # differences give exact 12 h (and 24 h) totals for verification windows. Saves ~4x the download.
    last = 360 if init.hour in (0, 12) else 144
    return [h for h in range(6, last + 1, 6) if (init.hour + h) % 12 == 0]


REGISTRY: dict[str, ModelSpec] = {
    # HRRR 48 h runs finish posting roughly 1h45-2h15 after init
    "HRRR": ModelSpec("HRRR", _hrrr_fxx, lag_hours=2.5),
    # GFS to 240 h finishes posting about 4h30-5h after init
    "GFS": ModelSpec("GFS", _gfs_fxx, lag_hours=5.5),
    # NAM 12 km to 84 h and NAM Nest 3 km to 60 h, 3-hourly; both post about 2 h after init
    "NAM": ModelSpec("NAM", lambda i, c: _steps((3, 84, 3)), lag_hours=2.5),
    "NAMNEST": ModelSpec("NAMNEST", lambda i, c: _steps((3, 60, 3)), lag_hours=2.5),
    # Canadian RDPS 10 km to 84 h (00/06/12/18z) and GDPS 15 km to 240 h (00/12z only); hourly files, we use 3 h
    # (6 h past day 5 for GDPS, whose global files are large)
    "RDPS": ModelSpec("RDPS", lambda i, c: _steps((3, 84, 3)), lag_hours=4.5),
    "GDPS": ModelSpec("GDPS", lambda i, c: _steps((3, 120, 3), (126, 240, 6)), lag_hours=5.5, only_cycles=(0, 12)),
    # ECMWF HRES ("IFS"); 00/12z to 360 h finish about 7-8 h after init
    "IFS": ModelSpec("IFS", _ifs_fxx, lag_hours=8.0),
    # Ensembles, precip only: GEFS 31 members to 384 h in 6 h buckets; ECMWF ENS 50 members
    "GEFS": ModelSpec("GEFS", lambda i, c: _steps((6, 384, 6)), lag_hours=7.0, members=31),
    "ENS": ModelSpec("ENS", _ens_fxx, lag_hours=9.0, members=50),
}


# Models grouped by the server they come from. A scheduled pass runs one process per stream at the same time,
# so a slow source (e.g. Environment Canada) never holds up the others, and no single server gets hit harder.
STREAMS: dict[str, list[str]] = {
    "noaa": ["HRRR", "NAMNEST", "NAM", "GFS", "GEFS"],   # AWS (+ NCAR GDEX for GFS)
    "ecmwf": ["IFS", "ENS"],                             # ECMWF open data (Google mirror)
    "canada": ["RDPS", "GDPS"],                          # Environment Canada MSC Datamart
}


def stream_of(model: str) -> str:
    """The server stream a model comes from ('noaa', 'ecmwf', 'canada')."""
    return next(s for s, ms in STREAMS.items() if model in ms)


def get_puller(model: str, cfg: dict):
    """The model's puller, with its server's parallel downloads (config archive.download_workers) filled in.
    Imported lazily so offline code never needs herbie installed."""
    w = cfg["archive"]["download_workers"]
    workers = w[stream_of(model)]
    if model == "HRRR":
        from .hrrr import pull_run
        return partial(pull_run, workers=workers)
    if model == "GFS":
        from .gfs import pull_run
        return partial(pull_run, workers=workers, gdex_workers=w["gdex"])
    if model in ("RDPS", "GDPS"):
        from . import canada
        return partial(canada.pull_run, model, workers=workers)
    if model in ("NAM", "NAMNEST", "IFS"):
        from . import herbie_generic
        return partial(herbie_generic.pull_run, model, workers=workers, throttle=cfg["archive"]["throttle"])
    if model in ("GEFS", "ENS"):
        from . import herbie_generic
        return partial(herbie_generic.pull_ensemble, model, workers=workers, throttle=cfg["archive"]["throttle"])
    raise KeyError(f"No puller for model {model!r}. Add one in models/registry.py")


def recent_cycles(model: str, cfg: dict, now_utc: datetime, n: int = 2) -> list[datetime]:
    """The latest n cycles that should be fully posted by now_utc, newest first."""
    spec = REGISTRY[model]
    hours = cycles(cfg, model)
    # Start from "now minus the posting delay", rounded down to the hour, and walk back one hour at a time
    # until n hours that are model cycle times have been found.
    t = (now_utc - timedelta(hours=spec.lag_hours)).replace(minute=0, second=0, microsecond=0)
    out: list[datetime] = []
    while len(out) < n:
        if t.hour in hours:
            out.append(t)
        t -= timedelta(hours=1)
    return out


def cycles_between(model: str, cfg: dict, start: datetime, end: datetime) -> list[datetime]:
    """All archive cycles with start <= init <= end (oldest first), for backfills."""
    hours = set(cycles(cfg, model))
    t = start.replace(minute=0, second=0, microsecond=0)
    out = []
    while t <= end:
        if t.hour in hours:
            out.append(t)
        t += timedelta(hours=1)
    return out
