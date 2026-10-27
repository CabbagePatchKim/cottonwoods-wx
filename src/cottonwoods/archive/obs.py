"""Observation archive.

    data/archive/obs/snotel/ym=2026-11.parquet     hourly SNOTEL (UTC timestamps), merged and de-duplicated
    data/archive/obs/synoptic/ym=2026-11.parquet   hourly Synoptic/MesoWest stations (e.g. Alta Collins), same layout
    data/archive/obs/ghcn/ym=2026-11.parquet       daily NOAA coop stations (e.g. ALTA); time_utc = end of the obs day
    data/archive/obs/sounding/ym=2026-11.parquet   weather-balloon soundings (KSLC), every reported level
    data/manual/new_snow.csv                        hand-entered ski-area / UAC snowfall

SNOTEL values can be revised after the fact, so each run re-fetches a trailing window and later fetches
replace earlier ones for the same (station, element, hour). Synoptic stations are handled the same way.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import ARCHIVE_DIR, ENTRIES_DIR, MANUAL_DIR
from ..timefmt import LOCAL_TZ
from ..obs import ghcn, snotel, sounding, synoptic
from .io import read_df, suffix_for, write_df

KEY = ["station", "element", "time_utc"]  # these three together identify one observation


def snotel_dir(root: Path | None = None) -> Path:
    return (root or ARCHIVE_DIR) / "obs" / "snotel"


def synoptic_dir(root: Path | None = None) -> Path:
    return (root or ARCHIVE_DIR) / "obs" / "synoptic"


def ghcn_dir(root: Path | None = None) -> Path:
    return (root or ARCHIVE_DIR) / "obs" / "ghcn"


def sounding_dir(root: Path | None = None) -> Path:
    return (root or ARCHIVE_DIR) / "obs" / "sounding"


def to_utc(df: pd.DataFrame, tz_offset_hours: float) -> pd.DataFrame:
    """SNOTEL stamps are PACIFIC standard time (-8) all year, even in Utah (verified; see config snotel_settings)."""
    out = df.copy()
    # local = UTC + offset, so UTC = local - offset. With offset -8: 00:00 PST - (-8 h) = 08:00 UTC.
    out["time_utc"] = out["time"] - pd.Timedelta(hours=tz_offset_hours)
    return out.drop(columns=["time"])


def merge_into_store(new: pd.DataFrame, cfg: dict, root: Path | None = None, store: Path | None = None,
                     key: list[str] = KEY) -> int:
    """Write rows into month files, replacing older copies of the same row (by `key`; default station/element/hour)."""
    suffix = suffix_for(cfg["archive"]["format"])
    store = store or snotel_dir(root)
    new = new.copy()
    # One file per month keeps files small, and a re-fetch only rewrites the months it touched.
    new["ym"] = new["time_utc"].dt.strftime("%Y-%m")
    total = 0
    for ym, part in new.groupby("ym"):
        path = store / f"ym={ym}{suffix}"
        part = part.drop(columns=["ym"])
        if path.exists():
            part = pd.concat([read_df(path), part], ignore_index=True)
        # Old and new rows together, sorted oldest fetch first. drop_duplicates(keep="last") then keeps the
        # NEWEST copy of each observation, which is how a revised value replaces the earlier one.
        part = part.sort_values("fetched_at").drop_duplicates(key, keep="last").sort_values(key)
        write_df(part, path)
        total += len(part)
    return total


def archive_snotel(cfg: dict, begin: str, end: str, root: Path | None = None, fetcher=None) -> int:
    """Fetch SNOTEL for [begin, end] (YYYY-MM-DD) for all configured stations and merge into the archive."""
    sites = {k: v["triplet"] for k, v in cfg["snotel"].items() if v.get("triplet")}
    if not sites:
        raise RuntimeError("No SNOTEL triplets set in config/stations.yaml (run scripts/01_find_stations.py).")
    df = (fetcher or snotel.fetch)(list(sites.values()), begin, end)
    if df.empty:
        return 0
    df = to_utc(df, cfg["snotel_settings"]["tz_offset_hours"])
    # Reverse the dict ({"766:UT:SNTL": "snowbird", ...}) and use .map to add the friendly site name to each row.
    by_trip = {v: k for k, v in sites.items()}
    df["site"] = df["station"].map(by_trip)
    df["fetched_at"] = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    return merge_into_store(df, cfg, root)


def archive_synoptic(cfg: dict, begin: str, end: str, root: Path | None = None, fetcher=None) -> int:
    """Fetch Synoptic stations (config `synoptic:`) for [begin, end] UTC and merge into the archive."""
    sites = {k: v["stid"] for k, v in (cfg.get("synoptic") or {}).items() if v.get("stid")}
    if not sites:
        return 0
    df = (fetcher or synoptic.fetch)(list(sites.values()), begin, end)
    if df.empty:
        return 0
    by_stid = {v: k for k, v in sites.items()}
    df["site"] = df["station"].map(by_stid)
    df["fetched_at"] = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    return merge_into_store(df, cfg, store=synoptic_dir(root))


def archive_ghcn(cfg: dict, begin: str, end: str, root: Path | None = None, fetcher=None) -> int:
    """Fetch NOAA GHCN-Daily stations (config `ghcn:`) for [begin, end] and merge into the archive."""
    sites = cfg.get("ghcn") or {}
    tz = cfg["local_settings"]["standard_offset_hours"]  # coop observers keep Utah local standard time (MST)
    total = 0
    # One request per station, because each can have its own observation hour.
    for name, v in sites.items():
        df = (fetcher or ghcn.fetch)([v["station"]], begin, end, obs_hour_local=v.get("obs_hour_local", 18),
                                     tz_offset_hours=tz)
        if df.empty:
            continue
        df["site"] = name
        df["fetched_at"] = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
        total += merge_into_store(df, cfg, store=ghcn_dir(root))
    return total


def archive_soundings(cfg: dict, begin: str, end: str, root: Path | None = None, fetcher=None) -> int:
    """Fetch balloon soundings (config `soundings:`) for [begin, end] UTC and merge into the archive.

    Each (station, launch time, pressure, level type) is one row; re-fetching replaces it, so a sounding
    that arrives late or gets corrected is picked up on the next run.
    """
    total = 0
    for name, v in (cfg.get("soundings") or {}).items():
        df = (fetcher or sounding.fetch)(v["station"], begin, end)
        if df.empty:
            continue
        df["site"] = name
        df["fetched_at"] = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
        total += merge_into_store(df, cfg, store=sounding_dir(root),
                                  key=["station", "time_utc", "pressure_hpa", "levelcode"])
    return total


def _load_store(base: Path, start=None, end=None) -> pd.DataFrame:
    if not base.exists():
        return pd.DataFrame()
    files = [p for p in base.glob("ym=*.*") if p.suffix in (".parquet", ".csv")]
    frames = [read_df(p) for p in sorted(files)]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if start is not None:
        df = df[df["time_utc"] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df["time_utc"] <= pd.Timestamp(end)]
    return df.reset_index(drop=True)


def load_snotel(start=None, end=None, root: Path | None = None) -> pd.DataFrame:
    return _load_store(snotel_dir(root), start, end)


def load_synoptic(start=None, end=None, root: Path | None = None) -> pd.DataFrame:
    return _load_store(synoptic_dir(root), start, end)


def drop_unreliable_water(obs: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Remove NOAA PRCP rows from before each station's `water_reliable_from` date (config `ghcn:`).

    Snowfall and depth rows are kept: only the water measurement is suspect in those years.
    Use this before anything that relies on observed water (verification, snow-ratio fitting).
    """
    if obs.empty or "site" not in obs.columns:
        return obs
    bad = pd.Series(False, index=obs.index)
    for site, v in (cfg.get("ghcn") or {}).items():
        if v.get("water_reliable_from"):
            bad |= (obs["site"] == site) & (obs["element"] == "PRCP") & (obs["time_utc"] < pd.Timestamp(v["water_reliable_from"]))
    return obs[~bad]


def load_ghcn(start=None, end=None, root: Path | None = None) -> pd.DataFrame:
    return _load_store(ghcn_dir(root), start, end)


def load_soundings(start=None, end=None, root: Path | None = None) -> pd.DataFrame:
    """Every archived balloon level. Use cottonwoods.obs.sounding.summarize() for one row per sounding."""
    return _load_store(sounding_dir(root), start, end)


def load_obs(start=None, end=None, root: Path | None = None) -> pd.DataFrame:
    """All observed sites (SNOTEL + Synoptic + NOAA) in one frame, told apart by the `site` column."""
    # The stores share the same core columns, so they can be stacked into one table.
    loaded = (load_snotel(start, end, root), load_synoptic(start, end, root), load_ghcn(start, end, root))
    frames = [f for f in loaded if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


STORM_TOTAL_COLS = ["start", "end", "site", "source", "new_snow_in", "water_in", "notes"]


def _local_to_utc(text: str) -> pd.Timestamp:
    """'2026-10-31 14:00' Mountain time -> naive UTC. A date alone means 0600, the morning report time."""
    text = str(text).strip()
    t = pd.Timestamp(text) + (pd.Timedelta(hours=6) if len(text) <= 10 else pd.Timedelta(0))
    return t.tz_localize(LOCAL_TZ, ambiguous=False, nonexistent="shift_forward").tz_convert("UTC").tz_localize(None)


def load_storm_totals(path: Path | None = None) -> pd.DataFrame:
    """data/entries/storm_totals.csv: storm totals entered by hand (e.g. Alta Collins, read off the NWS page by the
    owner). start/end are Mountain local time; returned with start_utc/end_utc added for matching to storms.
    data/entries is not in git (entries mustn't need a code release) and is synced through the bucket."""
    p = path or (ENTRIES_DIR / "storm_totals.csv")
    empty = pd.DataFrame(columns=STORM_TOTAL_COLS + ["start_utc", "end_utc"])
    if not p.exists():
        return empty
    df = pd.read_csv(p, dtype={"start": str, "end": str, "site": str, "source": str, "notes": str})
    df = df.dropna(subset=["start", "end", "site"])
    if df.empty:
        return empty
    df["site"] = df["site"].str.strip().str.lower()
    df["source"] = df["source"].fillna("manual").str.strip().str.lower()
    df["start_utc"] = [_local_to_utc(t) for t in df["start"]]
    df["end_utc"] = [_local_to_utc(t) for t in df["end"]]
    return df


# Column order of a hand-entered readings array (data/entries/collins_obs.npy); a CSV uses the same names as headers.
OBS_ENTRY_COLS = ["year", "month", "day", "hour", "period_h", "new_snow_in", "water_in", "depth_in", "temp_f", "rh_pct"]


def load_hand_obs(folder: Path | None = None, name: str = "collins_obs") -> pd.DataFrame:
    """Readings copied by hand (Alta Collins, from the NWS page by eye): <name>.npy (columns in OBS_ENTRY_COLS order)
    and/or <name>.csv (same names as headers). Each row is one PERIOD ending at year-month-day hour, Mountain local:
    hourly (period_h 1) or board readings (12, 24). Amounts are totals for that period; NaN = not read.
    Returns end_utc, start_utc and the values; a reading entered twice keeps the last one."""
    folder = folder or ENTRIES_DIR
    frames = []
    if (folder / f"{name}.npy").exists():
        arr = np.atleast_2d(np.load(folder / f"{name}.npy"))
        if arr.shape[1] != len(OBS_ENTRY_COLS):
            raise ValueError(f"{name}.npy has {arr.shape[1]} columns; expected {len(OBS_ENTRY_COLS)}: {OBS_ENTRY_COLS}")
        frames.append(pd.DataFrame(arr, columns=OBS_ENTRY_COLS))
    if (folder / f"{name}.csv").exists():
        frames.append(pd.read_csv(folder / f"{name}.csv")[OBS_ENTRY_COLS])
    if not frames:
        return pd.DataFrame(columns=OBS_ENTRY_COLS + ["end_utc", "start_utc"])
    df = pd.concat(frames, ignore_index=True).dropna(subset=["year", "month", "day", "hour", "period_h"])
    bad = df[(df["hour"] < 0) | (df["hour"] > 23) | (df["period_h"] <= 0) | (df["period_h"] > 72)
             | (df[["new_snow_in", "water_in", "depth_in"]] < 0).any(axis=1)]
    if len(bad):
        raise ValueError(f"{name}: impossible values in rows {list(bad.index)} (hour 0-23, period 1-72 h, no negative amounts)")
    local = pd.to_datetime(dict(year=df["year"], month=df["month"], day=df["day"], hour=df["hour"]))
    df["end_utc"] = local.dt.tz_localize(LOCAL_TZ, ambiguous=False, nonexistent="shift_forward") \
        .dt.tz_convert("UTC").dt.tz_localize(None)
    df["start_utc"] = df["end_utc"] - pd.to_timedelta(df["period_h"], unit="h")
    return df.drop_duplicates(subset=["end_utc", "period_h"], keep="last").sort_values(["end_utc", "period_h"]) \
        .reset_index(drop=True)


def load_manual(path: Path | None = None) -> pd.DataFrame:
    p = path or (MANUAL_DIR / "new_snow.csv")
    if not p.exists():
        return pd.DataFrame(columns=["date", "site", "new_snow_in", "swe_in", "notes"])
    df = pd.read_csv(p)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    # .str applies string methods to every value: " Alta " -> "alta", so hand-typed names still match.
    df["site"] = df["site"].astype(str).str.strip().str.lower()
    return df
