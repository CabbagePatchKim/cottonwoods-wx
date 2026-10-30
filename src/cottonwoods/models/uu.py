"""University of Utah (Steenburgh group) Little Cottonwood guidance at Alta Collins (owner, 2026-10-30).

EXPERIMENTAL, AUTOMATED AND UNOFFICIAL (their words); internal only, never in the feed; credit the Steenburgh group,
Department of Atmospheric Sciences, University of Utah. Config `uu` holds the addresses.
  UUHRRR / UUGFS  their HRRR (hourly to 48 h) and GFS (hourly to 120 h, 3-hourly to 180 h) at Collins, with a
                  machine-learning snow ratio: one text file per run (files kept back to 2026-01). Read like a model
                  run at the Alta point: their water and snow totals since init become steps; their snow is kept as
                  snow_issued_in (derive_run_fields makes it snow_in), with their SLR (slr_issued) and wet-bulb-zero
                  height (wbz_msl_m).
  Utah Snow Ensemble  82 members, percentiles (min, 10, 25, 50, 75, 90, max) every 6 h to 10 days at the grid point
                  nearest Collins: water, snow, SLR, wet-bulb 0.5 height. Only the newest run is posted, so each pass
                  saves it (archive_use) to data/archive/uu_ensemble/init=YYYYMMDDHH.csv.
"""
from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import ARCHIVE_DIR
from .accum import deaccumulate

FT_TO_M = 0.3048
MM_PER_IN = 25.4
# a data row starts with the forecast hour and a Mountain valid time, e.g. "  1  07 PM Thu Oct 29 2026"
ROW = re.compile(r"^\s*(\d+)\s+\d\d (AM|PM) \w{3} \w{3} \d\d \d{4}\s")
# wet-bulb-zero ft, SLR, step water/snow, totals since init (the two files lay the tail out differently). A wet-bulb
# zero at or below the Salt Lake airport is printed "≤ KSLC" (GFS) or "<KSLC" (HRRR), common in cold storms: kept as
# NaN (an upper bound only)
_WBZ = r"(?:(\d+) ?FT|[≤<] ?KSLC)"
_H2O = r"(?:\d+%|-)"     # snow water content; "-" when their snow ratio is 0 ("0:1/-")
TAILS = {
    "UUHRRR": re.compile(_WBZ + r"\s+(\d+):1/\s*" + _H2O + r"\s+[\d.]+/([\d.]+)\s+[\d.]+/\s*([\d.]+)\s*$"),
    "UUGFS": re.compile(_WBZ + r"\s+(\d+):1 /\s*" + _H2O + r"\s+[\d.]+\"\s*/\s*[\d.]+\"\s+([\d.]+)\"\s*/\s*([\d.]+)\"\s*$"),
}


def parse_text(model: str, text: str, init: datetime, point: str = "alta") -> pd.DataFrame:
    """One run's rows (model-run columns) from a guidance text file: steps between its forecast hours."""
    hours = {}
    for line in text.splitlines():
        m, t = ROW.match(line), TAILS[model].search(line)
        if m and t:
            wbz, slr, water, snow = t.groups()
            hours[int(m.group(1))] = (float(water), float(snow), int(slr), int(wbz) * FT_TO_M if wbz else np.nan)
    hours.pop(0, None)                                   # hour 0: nothing has fallen yet
    if not hours:
        return pd.DataFrame()
    fh = sorted(hours)
    water = deaccumulate([(0, f, hours[f][0]) for f in fh])
    snow = {(a, b): v for a, b, v in deaccumulate([(0, f, hours[f][1]) for f in fh])}
    t0 = pd.Timestamp(init)
    return pd.DataFrame([{"model": model, "init": t0, "lead_hr": b, "valid_start": t0 + pd.Timedelta(hours=a),
                          "valid_end": t0 + pd.Timedelta(hours=b), "point": point, "grid_dist_km": np.nan,
                          "qpf_mm": w * MM_PER_IN, "snow_issued_in": snow.get((a, b), np.nan),
                          "slr_issued": float(hours[b][2]), "wbz_msl_m": hours[b][3]} for a, b, w in water])


def pull_run(model: str, init: datetime, fxx_list, points: dict[str, dict], save_dir: str | None = None, *,
             cfg: dict) -> pd.DataFrame:
    """A guidance run (UUHRRR / UUGFS) at the Alta point: the text file for that init, parsed."""
    import requests
    uc = cfg["uu"]
    url = f"{uc['guidance_url']}{uc['files'][model]}_{init:%Y%m%d%H}.txt"
    r = requests.get(url, timeout=uc["timeout_s"])
    if r.status_code == 404:
        raise RuntimeError(f"{model} {init:%Y-%m-%d %H}z: not posted ({url})")
    r.raise_for_status()
    df = parse_text(model, r.text, init, next(iter(points)))
    return df[df["lead_hr"].isin([int(f) for f in fxx_list])] if not df.empty else df


# --- the Utah Snow Ensemble -------------------------------------------------------------------------------------

USE_VARS = {"Total Water": "water_total_in", "Total Snowfall": "snow_total_in", "6-h Water": "water_6h_in",
            "6-h Snowfall": "snow_6h_in", "Snow-to-Liquid": "slr", "Wet-Bulb 0.5": "wb05_msl_ft"}


class _Tables(HTMLParser):
    """Collects every <h2> title and every table's cell text, in order."""

    def __init__(self):
        super().__init__()
        self.titles, self.tables, self._cell, self._row, self._in = [], [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "h2":
            self._in, self._cell = "h2", []
        elif tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "br" and self._in == "h2":
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "h2":
            self.titles.append("".join(self._cell).strip())
            self._in = None
        elif tag in ("td", "th") and self._row is not None:
            self._row.append("".join(self._cell).strip())
        elif tag == "tr" and self._row is not None and self.tables:
            self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_use(html: str, tz: str = "America/Denver") -> tuple[pd.Timestamp, pd.DataFrame]:
    """(init UTC, long table time_utc / variable / stat / value) from the Utah Snow Ensemble page."""
    p = _Tables()
    p.feed(html)
    m = re.search(r"Initialized (\d{4}) UTC (\d+ \w{3} \d{4})", p.titles[0] if p.titles else "")
    if not m:
        raise ValueError("Utah Snow Ensemble: no 'Initialized HHMM UTC' in the page title")
    init = pd.to_datetime(f"{m.group(2)} {m.group(1)}", format="%d %b %Y %H%M")
    rows = []
    for title, table in zip(p.titles, p.tables):
        var = next((v for k, v in USE_VARS.items() if k in title), None)
        if var is None or len(table) < 2:
            continue
        times = [pd.to_datetime(h, format="%I %p %a %d %b %Y").tz_localize(tz).tz_convert("UTC").tz_localize(None)
                 for h in table[0][1:]]
        for r in table[1:]:
            stat = r[0].lower()
            for t, v in zip(times, r[1:]):
                try:
                    rows.append({"time_utc": t, "variable": var, "stat": stat, "value": float(v.replace("\\", ""))})
                except ValueError:
                    continue                             # a blank or non-number cell stays out
    return init, pd.DataFrame(rows)


def use_dir(root: Path | None = None) -> Path:
    return (root or ARCHIVE_DIR) / "uu_ensemble"


def archive_use(cfg: dict, root: Path | None = None, fetch=None) -> tuple[pd.Timestamp, str]:
    """Save the newest Utah Snow Ensemble run if it isn't stored yet: (init, 'archived' or 'exists')."""
    if fetch is None:
        import requests
        r = requests.get(cfg["uu"]["ensemble_url"], timeout=cfg["uu"]["timeout_s"])
        r.raise_for_status()
        html = r.text
    else:
        html = fetch()
    init, df = parse_use(html)
    path = use_dir(root) / f"init={init:%Y%m%d%H}.csv"
    if path.exists():
        return init, "exists"
    if df.empty:
        raise ValueError(f"Utah Snow Ensemble {init:%Y-%m-%d %H}z: no values parsed")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return init, "archived"


def use_over(df: pd.DataFrame, s, e) -> pd.DataFrame:
    """APPROXIMATE storm-window percentiles from a run's totals since init: each statistic's total at the last 6 h time
    at or before e minus at the last one at or before s (rows water / snow, columns min .. max). A percentile of a
    difference isn't the difference of percentiles, so this is a guide, not the members' storm totals; NaN columns
    where the run doesn't reach the storm."""
    out = {}
    for var, name in (("water_total_in", "water"), ("snow_total_in", "snow")):
        d = df[df["variable"] == var].pivot_table(index="time_utc", columns="stat", values="value")
        a, b = d.index[d.index <= s], d.index[d.index <= e]
        if len(b) == 0 or b.max() < e - pd.Timedelta(hours=6):
            continue                                     # the run ends well before the storm does
        start = d.loc[a.max()] if len(a) else d.iloc[0] * 0
        out[name] = (d.loc[b.max()] - start).clip(lower=0)
    stats = [c for c in ("min", "p10", "p25", "p50", "p75", "p90", "max")]
    return pd.DataFrame(out).T.reindex(columns=stats) if out else pd.DataFrame()


def load_use(init=None, root: Path | None = None) -> tuple[pd.Timestamp | None, pd.DataFrame]:
    """A stored Utah Snow Ensemble run (the newest if init is None): (init, long table)."""
    files = sorted(use_dir(root).glob("init=*.csv"))
    if init is not None:
        files = [f for f in files if f.stem == f"init={pd.Timestamp(init):%Y%m%d%H}"]
    if not files:
        return None, pd.DataFrame()
    f = files[-1]
    return pd.to_datetime(f.stem.split("=")[1], format="%Y%m%d%H"), pd.read_csv(f, parse_dates=["time_utc"])
