"""Paths and config loading."""
# Lets type hints like `Path | None` work on older Pythons. You'll see this line at the top of most modules.
from __future__ import annotations

import os
from pathlib import Path

import yaml

# __file__ is this file's own path: .../src/cottonwoods/config.py. parents[0] is src/cottonwoods,
# parents[1] is src, parents[2] is the project folder. Every path below hangs off it, so scripts work
# no matter which folder you run them from.
ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "stations.yaml"
# The data folder. Normally the project's own data/; the PRODUCTION copy (an approved release in its own folder,
# see CLAUDE.md "Website feed") sets COTTONWOODS_DATA to the development folder's data/, so both share one archive.
DATA_DIR = Path(os.environ.get("COTTONWOODS_DATA") or ROOT / "data")
JOURNAL_DIR = DATA_DIR / "journal"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
ARCHIVE_DIR = DATA_DIR / "archive"
MANUAL_DIR = DATA_DIR / "manual"
ENTRIES_DIR = DATA_DIR / "entries"        # owner's hand entries the cloud passes need (bucket-synced, not in git)
REPORTS_DIR = DATA_DIR / "reports"
FEED_DIR = DATA_DIR / "feed"              # the published website feed (approved releases only)
FEED_PREVIEW_DIR = DATA_DIR / "feed_preview"  # development previews of the feed, never published


# Settings the code reads directly, by section. The config file is the ONLY source of these values: the code has
# no hidden fallbacks, so a missing or misspelled setting stops the program with a clear message instead of quietly
# using a default that may have drifted from what the config says.
REQUIRED = {
    "archive": ["format", "models", "cycles_utc", "hrrr_max_fxx", "gfs_max_fxx", "gfs_step_hours", "recent_cycles",
                "download_workers", "throttle"],
    "kuchera": ["top_level_hpa", "bottom_level_hpa", "slr_floor", "slr_cap"],
    "precip_type": ["snow_level_tw_c", "mix_low_tw_c", "mix_high_tw_c", "rh_moist", "rh_dry", "max_nudge", "snow_min_slr"],
    "regimes": ["cold_700mb_c", "temp_bands_700mb_c", "lake_delta_c", "flow_bins", "other_label", "min_phase_hours", "factor_sites",
                "factor_min_days", "front_drop_c", "front_veer_deg", "front_window_hours", "cellular_cape_jkg",
                "cellular_lapse_c_per_km", "event_gap_hours", "event_wet_mm", "lake_flow_deg", "lake_min_rh700",
                "lake_temp_c_by_month"],
    "storms": ["point", "gap_hours", "wet_in", "obs_wet_in", "min_total_in", "subperiod_gap_hours",
               "manual_match_hours", "report_verified_days", "max_lead_hours"],
    "report": ["days", "deterministic_lead_hours", "ensemble_lead_hours"],
    "verify": ["window_hours", "window_end_utc_hour", "swe_tolerance_hours", "event_swe_in", "snow_bins_in",
               "snow_bins_above", "bin_score", "bin_score_leads_hours", "pillow_check"],
    "snotel_settings": ["tz_offset_hours"],
    "obs_fetch": ["retries", "retry_wait_s", "catchup_max_days"],
    "gauge": ["median_hours", "reset_drop_in"],
    "local_settings": ["standard_offset_hours"],
    "points": [],
    "snotel": [],
}


class ConfigError(ValueError):
    pass


def check_config(cfg: dict) -> dict:
    """Raise ConfigError naming every missing section/setting (all at once, not one per run)."""
    missing = [f"{sec}:" for sec in REQUIRED if sec not in cfg]
    missing += [f"{sec}.{key}" for sec, keys in REQUIRED.items() if sec in cfg for key in keys if key not in (cfg[sec] or {})]
    if missing:
        raise ConfigError("config/stations.yaml is missing: " + ", ".join(missing))
    return cfg


def load_config(path: Path | None = None) -> dict:
    """Read stations.yaml into nested dicts/lists, e.g. cfg["points"]["alta"]["lat"] -> 40.59, and check it."""
    # `path or CONFIG_PATH`: use the path passed in, or the default if none was given (tests pass their own).
    # `with open(...)` closes the file automatically, even if reading fails.
    with open(path or CONFIG_PATH) as f:
        return check_config(yaml.safe_load(f))


def ensure_dirs() -> None:
    # parents=True also makes missing parent folders; exist_ok=True means no error if it's already there.
    for d in (RAW_DIR, PROCESSED_DIR, ARCHIVE_DIR, MANUAL_DIR):
        d.mkdir(parents=True, exist_ok=True)
