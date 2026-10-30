"""The website feed: a static JSON "API" written after each pass, from the same numbers as the text reports.

Files (feed format v1; SCHEMA_VERSION below), written to <feed folder>/v1/:
  meta.json    when/what made it, code version, points, newest run of each model, warnings, units, credits
  storms.json  every tracked upcoming/ongoing storm: window, notes, each model's newest-run totals per resort
  daily.json   each model's newest run, day by day per resort (days end 0600 MDT / 0500 MST)

v1 publishes MODEL-RUN DATA ONLY (owner, 2026-10-27): no observations (SNOTEL, NOAA ALTA, Collins, soundings) and
no verification (verified storms, observed totals, scores). Those are still developed and used inside the program,
but publishing them is a later, deliberate release. model_only() enforces this every time the feed is built.

Format rules (the website relies on them; tests/test_feed.py pins them):
  - Within v1, fields may be ADDED but never renamed, removed or changed in meaning. A breaking change is a new
    v2 folder published alongside v1 until the site has switched.
  - Times are ISO 8601 in Mountain time with the offset ("2026-10-30T12:00:00-06:00"); model runs also carry
    their cycle name ("10/27 06z").
  - Amounts are inches (water, snow), snow levels feet, rounded for display; missing values are null.
  - Deterministic snow comes twice: snow_in (plain Kuchera) and snow_in_regime (each step scaled by the resort's
    history in that step's flow). Since v1.4 config feed.regime_snow is false: snow_in_regime is always null and
    snow_regime_adjusted false (the key stays, so the v1 format is unchanged); the adjustment is internal only.
  - A run that ends before the storm does, or starts after it begins (covers_full_storm false), has null amounts at
    every resort: its partial total isn't a forecast for the storm, and a 0 would read like one (a run made after a
    storm ended would otherwise show 0 for it). run_ends says how far it reached.
  - Lead limits (config): storms use each run's first 168 h only (run_ends is at most init + 168 h); daily.json
    has null deterministic amounts past 168 h and ensemble amounts to 240 h (the 8-10 day "x of N" tail).
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

import pandas as pd

from . import timefmt
from .units import FT_PER_M
from .report import model_days, recent_changes, regime_adjusted, storm_models, storm_notes

SCHEMA_VERSION = 1
DISCLAIMER = ("Experimental, automated model guidance for the Cottonwood Canyons. Not an official forecast; "
              "check the NWS and the Utah Avalanche Center before heading out.")
CREDITS = ["NOAA/NCEP (HRRR, NAM, GFS, GEFS) via NOAA Open Data Dissemination",
           "ECMWF open data (IFS, ENS), CC BY 4.0",
           "Environment and Climate Change Canada / MSC Datamart (RDPS, GDPS), Open Government Licence - Canada",
           "NCAR GDEX GFS archive, CC BY 4.0, doi:10.5065/D65D8PWK"]
REGIME_CREDIT = ("snow_in_regime adjustment factors: USDA NRCS SNOTEL history and Salt Lake City soundings "
                 "(Iowa Environmental Mesonet), 2010-2026")
# Keys that would mean observations or verification leaked into the feed (see the module docstring)
NOT_MODEL_DATA = re.compile(r"(^obs|observ|verif|actual|score|collins|snotel|ghcn|sounding|^final_|measured)", re.I)


def iso(ts) -> str | None:
    """A UTC time as Mountain-time ISO 8601 with offset, or None."""
    if ts is None or pd.isna(ts):
        return None
    return timefmt.local(ts).isoformat()


def _num(x, digits: int):
    """A rounded float, or None for missing (JSON has no NaN)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(float(x), digits) if digits > 0 else int(round(float(x), digits))


def _amounts(c: dict | None) -> dict | None:
    if c is None:
        return None
    if "members" in c:
        return {"type": "ensemble", "members_wet": c["members_wet"], "members": c["members"],
                "mean_water_in": _num(c["mean_water_in"], 2)}
    return {"type": "deterministic", "water_in": _num(c["water_in"], 2), "snow_in": _num(c["snow_in"], 1),
            "snow_in_regime": _num(c["snow_in_regime"], 1)}


def public_points(cfg: dict) -> list[str]:
    """The points the feed shows: every config point except `internal: true` ones (e.g. the SNOTEL-site points)."""
    return [p for p, v in cfg["points"].items() if not v.get("internal")]


def _run(init) -> dict:
    return {"cycle": f"{pd.Timestamp(init):%m/%d %H}z", "init": iso(init)}


def _feed_regime(fc: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, bool]:
    """regime_adjusted, only when config feed.regime_snow publishes it; otherwise snow_in_regime stays null."""
    return regime_adjusted(fc, cfg) if cfg["feed"]["regime_snow"] else (fc, False)


def build_storms(storms: pd.DataFrame, fc: pd.DataFrame, cfg: dict, now: pd.Timestamp) -> dict:
    """Storms still ahead or under way. One whose forecast window has ended leaves the feed (owner, 2026-10-28):
    it stays in storms.csv until observations verify it, but newer runs can no longer show it, so the site would
    only see ensembles and nulls for something that is over."""
    fc, adjusted = _feed_regime(fc, cfg)
    shown = public_points(cfg)
    out = []
    live = storms[(storms["status"] == "forecast") & (storms["fc_end"] > now)]
    for _, st in live.sort_values("fc_start").iterrows():
        models = []
        for r in storm_models(st, fc, cfg):
            sl = r["snow_level_m"]
            models.append({"model": r["model"], **_run(r["init"]), "covers_full_storm": r["full"],
                           "run_ends": iso(r["valid_to"]),
                           "snow_level_ft": {"start": _num(sl[0] * FT_PER_M, -2), "end": _num(sl[1] * FT_PER_M, -2)} if sl else None,
                           "flow": r["flow"] or None, "modes": r["modes"] or None,
                           "points": {p: _amounts(c) if r["full"] else None
                                      for p, c in r["points"].items() if p in shown}})
        out.append({"id": st.storm_id, "start": iso(st.fc_start), "end": iso(st.fc_end),
                    "hours": _num((st.fc_end - st.fc_start) / pd.Timedelta(hours=1), 0),
                    "window_from": {"model": st.defining_model, **_run(st.defining_init)},
                    "notes": [{"label": k, "text": v} for k, v in storm_notes(st)], "models": models})
    changes = [{"id": st.storm_id, "start": iso(st.fc_start), "status": st.status, "note": st.note}
               for _, st in recent_changes(storms).iterrows()]
    return {"snow_regime_adjusted": adjusted, "storms": out, "recent_changes": changes}


def build_daily(fc: pd.DataFrame, cfg: dict, start: pd.Timestamp, days: int | None = None) -> dict:
    fc, _ = _feed_regime(fc, cfg)
    points = {}
    for p in public_points(cfg):
        rows = model_days(fc, cfg, p, days, start)
        points[p] = [{"model": r["model"], **_run(r["init"]), "ensemble": bool(r["ensemble"]),
                      "days": [{"start": iso(s), "end": iso(e), "amounts": _amounts(c)}
                               for (s, e), c in zip(r["windows"], r["days"])]} for r in rows]
    return {"points": points}


def build_meta(fc: pd.DataFrame, cfg: dict, now: pd.Timestamp, code_version: str, warnings: list[str]) -> dict:
    newest = fc.groupby("model")["init"].max() if not fc.empty else pd.Series(dtype=object)
    return {"generated": iso(now), "code_version": code_version, "timezone": timefmt.LOCAL_TZ,
            "units": {"water": "in", "snow": "in", "snow_level": "ft"},
            "ensemble_wet_threshold_in": cfg["verify"]["event_swe_in"],
            "snow_method": "Kuchera snow ratio per model step from the profile at each resort's elevation, "
                           "wet-bulb snow/mix/rain, minimum ratio 3:1 (snow_in); coarse models blend the 4 grid "
                           "points around each resort",
            "snow_regime_method": ("snow_in_regime: each model step's snow scaled by the resort's precip share in "
                                   "that step's 700 mb flow and temperature, from SNOTEL + SLC balloons since 2010 "
                                   "(1.10 = 10% more than usual); unverified") if cfg["feed"]["regime_snow"] else
                                  "snow_in_regime: not published (always null); use snow_in",
            "points": {p: {k: cfg["points"][p][k] for k in ("lat", "lon", "elev_ft")} for p in public_points(cfg)},
            "models": [{"model": m, **_run(t)} for m, t in newest.items()],
            "warnings": warnings, "disclaimer": DISCLAIMER,
            "credits": CREDITS + [REGIME_CREDIT] if cfg["feed"]["regime_snow"] else CREDITS}


def model_only(feed: dict) -> dict:
    """The feed unchanged, or ValueError naming every key that looks like observations or verification (v1 is
    model-run data only). Checks keys at every depth; text values such as the method notes are not keys."""
    bad = []

    def walk(x, path):
        if isinstance(x, dict):
            for k, v in x.items():
                if NOT_MODEL_DATA.search(str(k)):
                    bad.append(f"{path}{k}")
                walk(v, f"{path}{k}.")
        elif isinstance(x, list):
            for v in x:
                walk(v, f"{path}[].")
    walk(feed, "")
    if bad:
        raise ValueError(f"feed v{SCHEMA_VERSION} is model-run data only; refusing to publish: {sorted(set(bad))[:10]}")
    return feed


def build_feed(storms: pd.DataFrame, fc: pd.DataFrame, cfg: dict, now: pd.Timestamp, code_version: str,
               warnings: list[str]) -> dict[str, dict]:
    """{"meta": ..., "storms": ..., "daily": ...}; every file carries schema_version and generated. Storms are the
    ones still ahead or under way only (ended and verified ones are observations' territory), and model_only()
    checks the rest. Only the models in config feed.models are published (owner, 2026-10-30: new models, guidance and
    ensembles reach the feed only through an approved release that adds them), and only their staleness warnings."""
    fc = fc[fc["model"].isin(cfg["feed"]["models"])]
    warnings = [w for w in warnings if any(w.startswith(f"WARNING {m}:") for m in cfg["feed"]["models"])]
    recent = fc[fc["init"] >= now - pd.Timedelta(days=2)]
    files = {"meta": build_meta(recent, cfg, now, code_version, warnings),
             "storms": build_storms(storms, fc, cfg, now),
             "daily": build_daily(recent, cfg, now)}
    return model_only({k: {"schema_version": SCHEMA_VERSION, "generated": iso(now), **v} for k, v in files.items()})


def write_feed(feed: dict[str, dict], folder: Path) -> Path:
    """Write <folder>/v1/<name>.json. Each file is written beside its target then swapped in, so a site reading
    mid-write never sees half a file."""
    out = Path(folder) / f"v{SCHEMA_VERSION}"
    out.mkdir(parents=True, exist_ok=True)
    for name, data in feed.items():
        tmp = out / f".{name}.json.tmp"
        tmp.write_text(json.dumps(data, indent=1, allow_nan=False), encoding="utf-8")
        os.replace(tmp, out / f"{name}.json")
    return out
