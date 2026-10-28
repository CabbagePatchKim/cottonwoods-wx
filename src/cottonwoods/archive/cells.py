"""Coarse models' resort points as a blend of the 2x2 grid cells around them (owner, 2026-10-28).

A coarse model (config model_cells) used to give each resort point its NEAREST grid point. Points a few km apart
could land in cells ~28 km apart (Solitude at Apex vs Mill-D North SNOTEL), each cell with its own profile and
precip, so a lower site could get more snow than a higher one. Now each point's pulled fields are the bilinear
blend of the four cells, weighted by the point's position between them (clipped at the block's edges), and the
derivation (profile at the point's real elevation, Kuchera, regimes) runs on that blended column.

The cell rows themselves stay as pulled, so the nearest-cell values can always be recovered, and blending again
gives the same result (re-deriving an archived run is safe). A step where any weighted cell is missing a value
keeps the point's nearest-cell value for that field.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

KEYS = ["lead_hr", "valid_start", "valid_end", "member"]          # one step of one run (member: ensembles)
NOT_BLENDED = KEYS + ["grid_dist_km"]                             # numbers that describe the row, not the weather


def cell_name(model: str, lat: float, lon: float) -> str:
    """e.g. "gfs_40.50N_111.75W": the name says exactly which grid point it is."""
    return f"{model.lower()}_{lat:.2f}N_{abs(lon):.2f}W"


def model_cells(model: str, cfg: dict) -> dict | None:
    """The model's 2x2 block {"lats": [..], "lons": [..]} (config model_cells; older configs: gfs_cells), or None."""
    return (cfg.get("model_cells") or {}).get(model) or (cfg.get("gfs_cells") if model == "GFS" else None)


def weights(model: str, cells: dict, lat: float, lon: float) -> dict[str, float]:
    """Bilinear weight of each cell (by name) for a point; a point outside the block takes its nearest edge."""
    (a, b), (c, d) = sorted(cells["lats"]), sorted(cells["lons"])
    fy = float(np.clip((lat - a) / (b - a), 0, 1))
    fx = float(np.clip((lon - c) / (d - c), 0, 1))
    return {cell_name(model, a, c): (1 - fy) * (1 - fx), cell_name(model, a, d): (1 - fy) * fx,
            cell_name(model, b, c): fy * (1 - fx), cell_name(model, b, d): fy * fx}


def blend_points(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Replace each configured point's pulled numbers with the bilinear blend of the model's four cells.
    Models without model_cells or with `blend: false` there (GEFS: its 0.5 deg block reaches the Salt Lake Valley),
    and runs missing a cell, come back unchanged. Adds cell_blend (bool) per row."""
    if df.empty or "model" not in df.columns:
        return df
    model = df["model"].iloc[0]
    cells = model_cells(model, cfg)
    if not cells or cells.get("blend") is False:
        return df
    names = [cell_name(model, la, lo) for la in cells["lats"] for lo in cells["lons"]]
    if not set(names) <= set(df["point"].unique()):
        return df
    keys = [k for k in KEYS if k in df.columns and df[k].notna().any()]   # an all-empty member column is no key
    num = [c for c in df.select_dtypes("number").columns if c not in NOT_BLENDED]
    cell_rows = df[df["point"].isin(names)]
    out = df.copy()
    out["cell_blend"] = False
    for p, pt in cfg["points"].items():
        mask = out["point"] == p
        if not mask.any():
            continue
        w = cell_rows["point"].map(weights(model, cells, pt["lat"], pt["lon"]))
        vals = cell_rows[num]
        by = [cell_rows[k] for k in keys]
        total = vals.mul(w, axis=0).groupby(by).sum()
        got = vals.notna().mul(w, axis=0).groupby(by).sum()           # weight that actually had a value
        blended = total.where(got > 1 - 1e-9)                         # any weighted cell missing -> NaN
        here = out.loc[mask]
        new = blended.reindex(pd.MultiIndex.from_frame(here[keys]) if len(keys) > 1 else pd.Index(here[keys[0]]))
        new.index = here.index
        out.loc[mask, num] = new.fillna(here[num])
        out.loc[mask, "cell_blend"] = new.notna().any(axis=1)
    return out
