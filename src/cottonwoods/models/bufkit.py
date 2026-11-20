"""BUFKIT model point soundings (owner, 2026-11-19): a run's full profile at every forecast hour at one station,
the raw material of a time-height section. Iowa State's MTArchive keeps NCEP's BUFKIT files (HRRR, RAP, GFS, RRFS;
NAM before its retirement) for Alta (40.58 -111.63, the model's ground ~2,930 m in HRRR) and KSLC, back to at least
2020 for Alta and 2016 for KSLC (checked 2026-11-19):
    {base_url}/YYYY/MM/DD/bufkit/HH/{folder}/{prefix}_{site}.buf     (config timeheight.files / sites)
Plain text (GEMPAK SNLIST layout): one block per forecast hour (station parameters such as CAPE, then the levels:
PRES TMPC TMWC DWPC THTE DRCT SKNT OMEG CFRL HGHT), then one surface table (P01M = model precip in the hour, mm).
Laptop / local only: not part of the scheduled pass.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

MISSING = -9999.0


def url(model: str, site: str, init: datetime, cfg: dict) -> str:
    tc = cfg["timeheight"]
    folder, prefix = tc["files"][model.upper()].split("/")
    return f"{tc['base_url']}/{init:%Y/%m/%d}/bufkit/{init:%H}/{folder}/{prefix}_{tc['sites'][site]}.buf"


def fetch(model: str, site: str, init: datetime, cfg: dict, cache_dir: Path | None = None) -> str:
    """The run's BUFKIT text (from cache_dir if saved before; saved there after a download)."""
    path = cache_dir / model.upper() / f"{site}_{init:%Y%m%d%H}.buf" if cache_dir else None
    if path is not None and path.exists():
        return path.read_text()
    tc = cfg["timeheight"]
    r = requests.get(url(model, site, init, cfg), timeout=tc["timeout_s"], headers={"User-Agent": tc["user_agent"]})
    if r.status_code == 404:
        raise RuntimeError(f"{model} {site} {init:%Y-%m-%d %H}z: no BUFKIT file ({url(model, site, init, cfg)})")
    r.raise_for_status()
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(r.text)
    return r.text


def _num(tok: str) -> float:
    v = float(tok)
    return np.nan if v == MISSING else v


def _time(s: str) -> pd.Timestamp:
    return pd.Timestamp(datetime.strptime(s, "%y%m%d/%H%M"))


def parse(text: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(levels, hours): levels = one row per (time, level), columns lower-case SNPARM names (pres, tmpc, tmwc, dwpc,
    thte, drct, sknt, omeg, cfrl, hght); hours = one row per time with the station parameters (cape, pwat, ...)
    and the surface table's columns (p01m, ...), lower-case. Times are UTC, naive."""
    snparm = [p.lower() for p in re.search(r"SNPARM\s*=\s*([\w;]+)", text).group(1).split(";")]
    stn_at = text.find("\nSTN ")
    body, surface = (text[:stn_at], text[stn_at:]) if stn_at >= 0 else (text, "")
    levels, params = [], []
    for block in re.split(r"\n\s*STID\s*=", body)[1:]:
        t = _time(re.search(r"TIME\s*=\s*(\d{6}/\d{4})", block).group(1))
        head, _, data = block.partition(snparm[-1].upper())          # the level table follows its header line
        stn = {k.lower(): _num(v) for k, v in re.findall(r"\b([A-Z]{4})\s*=\s*(-?[\d.]+)", head)
               if k not in ("STNM", "TIME", "SLAT", "SLON", "SELV", "STIM")}
        params.append({"time": t, **stn})
        vals = []
        for tok in data.split():
            try:
                vals.append(_num(tok))
            except ValueError:
                break
        n = len(snparm)
        for row in np.array(vals[:len(vals) // n * n]).reshape(-1, n):
            levels.append({"time": t, **dict(zip(snparm, row))})
    hours = pd.DataFrame(params)
    if surface:
        toks = surface.split()
        names = []
        for tok in toks:
            if re.fullmatch(r"-?\d+(\.\d+)?", tok):
                break
            names.append(tok.lower())
        rows = toks[len(names):]
        recs = [rows[i:i + len(names)] for i in range(0, len(rows) - len(names) + 1, len(names))]
        sfc = pd.DataFrame([{"time": _time(r[1]), **{k: _num(v) for k, v in zip(names[2:], r[2:])}} for r in recs])
        hours = hours.merge(sfc, on="time", how="left") if not hours.empty else sfc
    return pd.DataFrame(levels), hours
