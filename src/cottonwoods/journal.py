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


def storm_window(storm, tz: str = "America/Denver") -> tuple[pd.Timestamp, pd.Timestamp]:
    """A journal storm's window in UTC: its start date at midnight Mountain to the morning report (0600) after its
    end date. The journal has dates, not times (window_confirmed=false), so this is the widest fair reading."""
    s = pd.Timestamp(storm.start_date).tz_localize(tz).tz_convert("UTC").tz_localize(None)
    e = (pd.Timestamp(storm.end_date) + pd.Timedelta(days=1, hours=6)).tz_localize(tz).tz_convert("UTC").tz_localize(None)
    return s, e


def score_runs(fc: pd.DataFrame, storms: pd.DataFrame, cfg: dict, point: str = "alta") -> pd.DataFrame:
    """Archived runs against the journal's actuals (owner, 2026-10-30; e.g. the U of U guidance, whose files go back to
    2026-01): every deterministic or guidance run issued within verify.score_runs_hours before a journal storm with an
    actual, covering its whole window (storm_window), at `point`: storm_id, model, init, lead_hr, snow_in, actual_in,
    snow_err_in, and the owner's bin score (bins.bin_score, without the event call: the journal has snow only)."""
    from . import bins
    from .models.registry import kind_of
    from .windows import window_total
    rows = []
    fp = fc[(fc["point"] == point) & (fc["model"].map(kind_of) != "ensemble")] if not fc.empty else fc
    for _, st in storms.dropna(subset=["actual_in"]).iterrows():
        s, e = storm_window(st)
        near = fp[(fp["init"] <= s) & (fp["init"] >= s - pd.Timedelta(hours=cfg["verify"]["score_runs_hours"]))]
        for (model, init), g in near.groupby(["model", "init"]):
            if g["valid_start"].min() > s or g["valid_end"].max() < e or "snow_in" not in g:
                continue                                  # the run doesn't cover the whole storm
            snow = window_total(g, s, e, "snow_in")
            rows.append({"storm_id": st.storm_id, "model": model, "init": init, "lead_hr": (s - init) / pd.Timedelta(hours=1),
                         "snow_in": snow, "actual_in": float(st.actual_in), "snow_err_in": snow - float(st.actual_in),
                         **bins.bin_score(snow, float(st.actual_in), cfg["verify"])})
    return pd.DataFrame(rows)
