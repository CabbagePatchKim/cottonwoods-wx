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

# Board glitch (owner 2026-11-23): twice in the 11/19-21 storm the interval board jumped far up for two hours and came
# back, the depth hardly changing: the jump counted as snow, and the fall back as a cleared board (3 x the real total).
MAX_RISE_IN = 6.0         # a bigger rise in an hour is a glitch (real ones so far are well under it)...
DEPTH_SUPPORT = 0.5       # ...unless the snow depth rose by at least this share of it (or, without depth, the hours
                          # since the last good reading allow it: a Worker outage)

# Settling (owner 2026-11-23): a small drop that leaves more on the board than the hour's water could make is the snow
# settling, not a cleared board, so what's left is not new snow.
SETTLE_MIN_IN = 1.0       # a drop up to this, or up to SETTLE_SHARE of the board, can be settling (bigger = cleared)
SETTLE_SHARE = 0.2
SETTLE_SLR = 20.0         # ...if more is left than the hour's water x this
SETTLE_HOURS = 3          # back up to where it was within this many hours = the reading wobbled: not new snow either


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


def board_glitches(d: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """(glitch, kept) per stored hour, `d` sorted by time. glitch = the interval board jumped beyond MAX_RISE_IN with no
    matching depth rise; it lasts until the board is back down. kept = a glitch hour the board came back from at or above
    its last good value (not cleared meanwhile): the snow of those hours is in the rise to that value, so none is lost."""
    glitch, kept = pd.Series(False, index=d.index), pd.Series(False, index=d.index)
    last, run = None, []                                  # last good row; glitch hours since it
    for i, r in d.iterrows():
        if pd.isna(r.snow_interval_in):
            continue
        if last is not None:
            rise, deeper = r.snow_interval_in - last.snow_interval_in, r.depth_in - last.depth_in
            hours = (r.time_utc - last.time_utc) / pd.Timedelta(hours=1)
            real = deeper >= DEPTH_SUPPORT * rise if pd.notna(deeper) else rise <= MAX_RISE_IN * hours
            if rise > MAX_RISE_IN and not real:
                run.append(i)
                continue
            glitch[run] = True
            kept[run] = rise >= 0
        last, run = r, []
    glitch[run] = True                                    # still up at the newest hour: unknown for now
    return glitch, kept


def _rises(d: pd.DataFrame, board: pd.Series) -> pd.Series:
    """New snow per stored hour from the interval board (glitches already blank): its rise; after a clearing its new
    value; 0 for settling. NaN for the first reading and blank hours."""
    out = pd.Series(float("nan"), index=d.index)
    before = top = top_time = None                        # last reading; level before settling, and when it settled
    for i, v in board.items():
        if pd.isna(v):
            continue
        t, water = d.at[i, "time_utc"], d.at[i, "water_in"]
        if before is not None:
            if top is not None and t - top_time > pd.Timedelta(hours=SETTLE_HOURS):
                top = None
            left = v > (0.0 if pd.isna(water) else water) * SETTLE_SLR     # more on the board than fell this hour
            if v >= before:
                out[i] = max(0.0, v - (before if top is None else max(before, top)))
            elif left and before - v <= max(SETTLE_MIN_IN, SETTLE_SHARE * before):
                out[i] = 0.0
                top, top_time = max(before, top or 0.0), t
            else:
                out[i], top = v, None                     # cleared: snow since
        before = v
    return out


def readings(stored: pd.DataFrame) -> pd.DataFrame:
    """Stored hours -> Collins readings, the same columns as archive.obs.load_hand_obs (hourly, period_h 1):
    new_snow_in = the interval board's rise over the hour (after a clearing: its new value); NaN for the first hour
    stored and for hours the board is blank; settling counts 0 (_rises). Glitch hours (board_glitches) count 0 when the rise to the next good value
    covers them, else NaN; the stored values stay as read."""
    cols = ["end_utc", "start_utc", "period_h", "new_snow_in", "water_in", "depth_in", "temp_f", "rh_pct"]
    if stored is None or stored.empty:
        return pd.DataFrame(columns=cols)
    d = stored.sort_values("time_utc").reset_index(drop=True)
    glitch, kept = board_glitches(d)
    d["new_snow_in"] = _rises(d, d["snow_interval_in"].mask(glitch))
    d.loc[kept, "new_snow_in"] = 0.0
    d["end_utc"] = d["time_utc"]
    d["start_utc"] = d["end_utc"] - pd.Timedelta(hours=1)
    d["period_h"] = 1
    return d[cols]
