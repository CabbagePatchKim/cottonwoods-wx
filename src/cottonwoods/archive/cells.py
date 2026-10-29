"""Coarse models' resort points as a blend of the 2x2 grid cells around them (owner, 2026-10-28).

A coarse model (config model_cells) used to give each resort point its NEAREST grid point. Points a few km apart
could land in cells ~28 km apart (Solitude at Apex vs Mill-D North SNOTEL), each cell with its own profile and
precip, so a lower site could get more snow than a higher one. Now each point's pulled fields are the bilinear
blend of the four cells, weighted by the point's position between them (clipped at the block's edges), and the
derivation (profile at the point's real elevation, Kuchera, regimes) runs on that blended column.

The cell rows themselves stay as pulled, so the nearest-cell values can always be recovered, and blending again
gives the same result (re-deriving an archived run is safe). A step where any weighted cell is missing a value
keeps the point's nearest-cell value for that field.

A rotated grid (RDPS, 2026-10-29) can't be described by lat/lon lines: its block is config `grid_points`, the
model's own grid points row by row, and a point is blended inside the grid square around it (bilinear in the
square's own coordinates, found from its four corners).

The archive keeps both versions (owner, 2026-10-29): the blended run in data/archive/forecasts (what the reports and
the feed use) and the unmodified one, each point at its nearest grid point, in data/archive/forecasts_raw.
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


def blends(model: str, cfg: dict) -> bool:
    """True for a model whose points are blended (config model_cells without `blend: false`)."""
    cells = model_cells(model, cfg)
    return bool(cells) and cells.get("blend") is not False


def cell_points(model: str, cells: dict) -> dict[str, dict]:
    """{cell name: {"lat", "lon"}} for every grid point of the model's block."""
    if "grid_points" in cells:
        pts = [pt for row in cells["grid_points"] for pt in row]
    else:
        pts = [(la, lo) for la in cells["lats"] for lo in cells["lons"]]
    return {cell_name(model, la, lo): {"lat": la, "lon": lo} for la, lo in pts}


def _inverse_bilinear(q: np.ndarray, lat: float, lon: float) -> tuple[float, float]:
    """(fy, fx) of a point in the grid square q (2 x 2 corners of (lat, lon); [0, 0] south-west), 0-1 inside it."""
    p00, p01, p10, p11 = q[0, 0], q[0, 1], q[1, 0], q[1, 1]
    fy = fx = 0.5
    for _ in range(20):                                   # Newton steps: converges in a few for a near-square
        here = (1 - fy) * (1 - fx) * p00 + (1 - fy) * fx * p01 + fy * (1 - fx) * p10 + fy * fx * p11
        d_fy = (1 - fx) * (p10 - p00) + fx * (p11 - p01)
        d_fx = (1 - fy) * (p01 - p00) + fy * (p11 - p10)
        step = np.linalg.solve(np.column_stack([d_fy, d_fx]), np.array([lat, lon]) - here)
        fy, fx = fy + step[0], fx + step[1]
        if np.abs(step).max() < 1e-12:
            break
    return float(fy), float(fx)


def _grid_weights(model: str, cells: dict, lat: float, lon: float) -> dict[str, float]:
    """Bilinear weights inside the grid square (of config grid_points) holding the point; outside the block, the
    nearest square, clipped at its edge."""
    g = np.asarray(cells["grid_points"], float)
    best = None
    for r in range(g.shape[0] - 1):
        for c in range(g.shape[1] - 1):
            fy, fx = _inverse_bilinear(g[r:r + 2, c:c + 2], lat, lon)
            outside = np.hypot(max(0.0, -fy, fy - 1), max(0.0, -fx, fx - 1))   # 0 inside the square
            if best is None or outside < best[0]:
                best = (outside, r, c, fy, fx)
    _, r, c, fy, fx = best
    fy, fx = float(np.clip(fy, 0, 1)), float(np.clip(fx, 0, 1))
    name = lambda i, j: cell_name(model, *g[r + i, c + j])   # noqa: E731
    return {name(0, 0): (1 - fy) * (1 - fx), name(0, 1): (1 - fy) * fx, name(1, 0): fy * (1 - fx), name(1, 1): fy * fx}


def weights(model: str, cells: dict, lat: float, lon: float) -> dict[str, float]:
    """Bilinear weight of each cell (by name) for a point; a point outside the block takes its nearest edge."""
    if "grid_points" in cells:
        return _grid_weights(model, cells, lat, lon)
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
    if not blends(model, cfg):
        return df
    cells = model_cells(model, cfg)
    names = list(cell_points(model, cells))
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
        w = cell_rows["point"].map(weights(model, cells, pt["lat"], pt["lon"])).fillna(0.0)   # other cells: 0
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
