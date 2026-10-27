"""Load the snow journal and score it against observed totals."""
from __future__ import annotations

import pandas as pd

from .config import JOURNAL_DIR


def load_storms() -> pd.DataFrame:
    # parse_dates turns those text columns into real dates, so you can subtract/compare them.
    return pd.read_csv(JOURNAL_DIR / "storms.csv", parse_dates=["entry_date", "start_date", "end_date"])


def load_forecasts() -> pd.DataFrame:
    df = pd.read_csv(JOURNAL_DIR / "journal_forecasts.csv", parse_dates=["init_utc"])
    # Midpoint of the forecast range ("8-12" -> 10), used to score it as a single number.
    df["mid_in"] = (df["low_in"] + df["high_in"]) / 2
    return df


def score_journal() -> pd.DataFrame:
    """One row per journal forecast with error vs the actual (where an actual exists).

    Open-ended ("60+") and truncated forecasts are kept but flagged; check `notes` before trusting.
    """
    storms = load_storms()[["storm_id", "actual_in"]]
    # merge = SQL-style join: attach each forecast's storm actual. how="left" keeps every forecast, even
    # when the storm has no actual yet (actual_in becomes NaN, "not a number", pandas' missing value).
    fc = load_forecasts().merge(storms, on="storm_id", how="left")
    fc["error_in"] = fc["mid_in"] - fc["actual_in"]
    fc["ratio"] = fc["mid_in"] / fc["actual_in"]
    # These operate on whole columns at once (no loop needed). & is "and" for True/False columns; the
    # parentheses are required because & binds tighter than >= and <=.
    fc["hit"] = (fc["actual_in"] >= fc["low_in"]) & (fc["actual_in"] <= fc["high_in"])
    return fc
