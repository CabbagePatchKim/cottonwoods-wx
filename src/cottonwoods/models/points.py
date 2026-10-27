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


def nearest_index(lats, lons, lat0: float, lon0: float):
    """Return (index tuple, distance_km) of the grid cell nearest (lat0, lon0).

    lats/lons may be 1-D (regular grid) or 2-D (e.g. HRRR Lambert conformal).
    """
    lats = np.asarray(lats, float)
    lons = np.asarray(lons, float)
    lon0 = float(_norm_lon(lon0, lons))
    # GFS-style grids list each latitude and longitude once (1-D); meshgrid expands them into the full 2-D grid
    # of every (lat, lon) pair. HRRR's grid is already 2-D because its map projection curves the lines.
    if lats.ndim == 1 and lons.ndim == 1:
        la, lo = np.meshgrid(lats, lons, indexing="ij")
    else:
        la, lo = lats, lons
    # Distance from the target to EVERY grid cell at once (numpy does the whole array in one call),
    # then nanargmin finds the closest. unravel_index turns that flat position back into (row, col).
    d = _haversine_km(la, lo, lat0, lon0)
    idx = np.unravel_index(np.nanargmin(d), d.shape)
    return idx, float(d[idx])
