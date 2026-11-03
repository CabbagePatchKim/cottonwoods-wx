"""Alta Collins (CLN) hourly readings from the owner's Cloudflare Worker (owner, 2026-11-01).

The Worker serves the NWS CLN timeseries table as JSON: {"scraped": ISO UTC, "rows": [{header: text}, ...]}, rows
newest first, times local ("Nov 1, 12:00 am", no year, no zone). The owner chose this source knowing the page's data
comes from Synoptic through the NWS; the rule that comes with it (owner, 2026-11-01): RECORD, NEVER PUBLISH.
  - values go only to the private archive (data/archive/obs/collins, bucket-synced, not in git) and the private
    reports; never the feed, never the public Actions log (the pass prints counts and times only);
  - the Worker's address and key come from the environment (GitHub secrets), never the code or config.

Each hour gives water (1 Hour Precip), depth, temperature, RH, and the Snow Interval board's running value; new snow
per hour is the rise of that board (a drop = the board was cleared: the new value is snow since). The 3/6 h
snowfall columns come from the depth sensor and are not used; the 24 h board is left out (it covers a fixed day and
can catch only part of a storm: 10/31 it said 1" when the interval board had 2").
"""
from __future__ import annotations

import os

import pandas as pd

from ..timefmt import LOCAL_TZ

# our name -> the page's column header. A missing header fails loudly (the page layout changed).
COLUMNS = {"water_in": "1 Hour Precip (in)", "snow_interval_in": "Snow Interval (in)", "depth_in": "Snow Depth (in)",
           "temp_f": "Temp. (°F)", "rh_pct": "Relative Humidity (%)"}
TIME_COLUMN = "Date/Time (L)"


def source(cfg: dict) -> tuple[str | None, str | None]:
    """The Worker's address and key from the environment variables named in config collins_source (None if unset)."""
    c = cfg.get("collins_source") or {}
    return (os.environ.get(c.get("url_env", "")) or None), (os.environ.get(c.get("key_env", "")) or None)


def fetch(cfg: dict) -> dict:
    """The Worker's JSON. Errors never include the address (it is a secret)."""
    import requests
    c = cfg["collins_source"]
    url, key = source(cfg)
    if not url:
        raise RuntimeError(f"no Collins source address ({c['url_env']} not set)")
    try:
        r = requests.get(url, headers={c["key_header"]: key} if key else {}, timeout=c["timeout_s"])
    except requests.RequestException as e:
        raise RuntimeError(f"Collins source unreachable ({type(e).__name__})") from None
    if r.status_code != 200:
        raise RuntimeError(f"Collins source answered HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError("Collins source sent something that isn't JSON") from None


def _local_times(labels: list[str], scraped_utc: pd.Timestamp) -> pd.DatetimeIndex:
    """Page labels ("Nov 1, 12:00 am"), oldest first, -> naive UTC. The year is the scrape's (the year before for a
    label later than the scrape: a December row read in January). The repeated hour when DST ends is told apart by
    order (pandas ambiguous="infer"); if that can't be done, those hours are NaT and get dropped."""
    now_local = scraped_utc.tz_convert(LOCAL_TZ).tz_localize(None)
    t = pd.to_datetime([f"{now_local.year} {s}" for s in labels], format="%Y %b %d, %I:%M %p")
    t = pd.DatetimeIndex([x.replace(year=x.year - 1) if x > now_local + pd.Timedelta(days=1) else x for x in t])
    try:
        loc = t.tz_localize(LOCAL_TZ, ambiguous="infer", nonexistent="shift_forward")
    except Exception:  # noqa: BLE001 - pytz/zoneinfo raise different classes; either way: drop the doubtful hour
        loc = t.tz_localize(LOCAL_TZ, ambiguous="NaT", nonexistent="shift_forward")
    return loc.tz_convert("UTC").tz_localize(None)


def parse(payload: dict) -> pd.DataFrame:
    """The Worker's rows -> time_utc (end of the hour), water_in, snow_interval_in, depth_in, temp_f, rh_pct,
    fetched_at. Blank cells are NaN."""
    rows = payload.get("rows") or []
    out_cols = ["time_utc", *COLUMNS, "fetched_at"]
    if not rows:
        return pd.DataFrame(columns=out_cols)
    df = pd.DataFrame(rows)
    missing = [h for h in [TIME_COLUMN, *COLUMNS.values()] if h not in df.columns]
    if missing:
        raise ValueError(f"Collins source: page columns missing {missing} (has the NWS page layout changed?)")
    scraped = pd.Timestamp(payload["scraped"])
    scraped = scraped.tz_localize("UTC") if scraped.tzinfo is None else scraped.tz_convert("UTC")
    df = df.iloc[::-1].reset_index(drop=True)                     # page order is newest first -> oldest first
    out = pd.DataFrame({"time_utc": _local_times(df[TIME_COLUMN].tolist(), scraped)})
    for ours, header in COLUMNS.items():
        out[ours] = pd.to_numeric(df[header].replace("", None), errors="coerce")
    out["fetched_at"] = scraped.tz_localize(None)
    return out.dropna(subset=["time_utc"]).drop_duplicates("time_utc", keep="last")[out_cols].reset_index(drop=True)


def readings(stored: pd.DataFrame) -> pd.DataFrame:
    """Stored hours -> Collins readings, the same columns as archive.obs.load_hand_obs (hourly, period_h 1):
    new_snow_in = the interval board's rise over the hour (after a clearing: its new value); NaN for the first hour
    stored and for hours the board is blank."""
    cols = ["end_utc", "start_utc", "period_h", "new_snow_in", "water_in", "depth_in", "temp_f", "rh_pct"]
    if stored is None or stored.empty:
        return pd.DataFrame(columns=cols)
    d = stored.sort_values("time_utc").reset_index(drop=True)
    board, before = d["snow_interval_in"], d["snow_interval_in"].ffill().shift(1)
    d["new_snow_in"] = (board - before).where(board >= before, board).where(before.notna() & board.notna())
    d["end_utc"] = d["time_utc"]
    d["start_utc"] = d["end_utc"] - pd.Timedelta(hours=1)
    d["period_h"] = 1
    return d[cols]
