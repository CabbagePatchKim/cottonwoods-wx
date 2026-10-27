"""Nearest-grid-point and box helpers that work for 1-D (lat/lon) and 2-D (projected) grids."""
from __future__ import annotations

import numpy as np


def _norm_lon(lon, like):
    """Match longitude convention (0-360 vs -180-180) of the grid."""
    lon = np.asarray(lon, float)
    like = np.asarray(like, float)
    if np.nanmax(like) > 180.0:
        return lon % 360.0
    return ((lon + 180.0) % 360.0) - 180.0


# Great-circle distance on a sphere. Plain lat/lon differences aren't distances: a degree of longitude
# shrinks toward the poles (about 85 km here at 40 N vs 111 km at the equator).
def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(lon2) - np.radians(lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


# Nearest points already found on 2-D grids, keyed by the grid's shape and corners plus the target. A run reads the
# same grid hundreds of times (every field of every forecast hour), so the full search happens once per grid.
_CACHE: dict = {}


def nearest_index(lats, lons, lat0: float, lon0: float):
    """Return (index tuple, distance_km) of the grid cell nearest (lat0, lon0).

    lats/lons may be 1-D (regular grid) or 2-D (e.g. HRRR Lambert conformal).
    """
    lats = np.asarray(lats, float)
    lons = np.asarray(lons, float)
    lon0 = float(_norm_lon(lon0, lons))
    if lats.ndim == 1 and lons.ndim == 1:
        # GFS-style grids list each latitude and longitude once (1-D). The nearest row and column are found
        # directly; the true nearest cell is within one row/column of them, so only that 3x3 block is measured.
        # (Measuring all 2.9 million GDPS points for every field made one GDPS run take over an hour.)
        i0, j0 = int(np.nanargmin(np.abs(lats - lat0))), int(np.nanargmin(np.abs(lons - lon0)))
        rows = np.arange(max(i0 - 1, 0), min(i0 + 2, lats.size))
        cols = np.arange(max(j0 - 1, 0), min(j0 + 2, lons.size))
        la, lo = np.meshgrid(lats[rows], lons[cols], indexing="ij")
        d = _haversine_km(la, lo, lat0, lon0)
        k = np.unravel_index(np.nanargmin(d), d.shape)
        return (int(rows[k[0]]), int(cols[k[1]])), float(d[k])
    # HRRR's grid is already 2-D because its map projection curves the lines: measure every cell, once per grid.
    key = (lats.shape, float(lats.flat[0]), float(lats.flat[-1]), float(lons.flat[0]), float(lons.flat[-1]),
           round(lat0, 6), round(lon0, 6))
    if key not in _CACHE:
        # Distance from the target to EVERY grid cell at once (numpy does the whole array in one call),
        # then nanargmin finds the closest. unravel_index turns that flat position back into (row, col).
        d = _haversine_km(lats, lons, lat0, lon0)
        idx = np.unravel_index(np.nanargmin(d), d.shape)
        _CACHE[key] = (tuple(int(i) for i in idx), float(d[idx]))
    return _CACHE[key]
