"""Tiny table I/O layer so the archive can be parquet (default) or csv (no pyarrow)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

# CSV stores everything as text, so these columns must be turned back into timestamps when read.
# (Parquet keeps column types on its own, which is one reason it's the default.)
TIME_COLS = ("init", "valid_start", "valid_end", "time_utc", "fetched_at")


def suffix_for(fmt: str) -> str:
    if fmt not in ("parquet", "csv"):
        raise ValueError(f"archive.format must be 'parquet' or 'csv', got {fmt!r}")
    return ".parquet" if fmt == "parquet" else ".csv"


def write_df(df: pd.DataFrame, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temporary file first, then swap it into place in one step (below).
    tmp = path.with_name(path.name + ".tmp")
    if path.suffix == ".parquet":
        df.to_parquet(tmp, index=False)
    else:
        df.to_csv(tmp, index=False)
    tmp.replace(path)  # atomic-ish: a killed cron job never leaves a half-written archive file


def read_df(path: Path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    df = pd.read_csv(path)
    for c in TIME_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c])
    return df
