"""Snow-to-liquid ratio (SLR) and snowfall calculations.

Rule: SLR is applied per output time step, then summed. Never apply it to accumulated QPF.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

T_REF_K = 271.16  # about -2 C. Kelvin = Celsius + 273.15; the GRIB model data is in Kelvin.


def kuchera_slr(tmax_k, floor: float | None = None, cap: float | None = None):
    """Kuchera SLR from the maximum temperature (K) in the 850-500 mb layer.

    Tmax > 271.16 K:  SLR = 12 + 2 * (271.16 - Tmax)
    Tmax <= 271.16 K: SLR = 12 + (271.16 - Tmax)

    floor/cap optionally clamp the result (None = unclamped). The result never goes below 0: above ~277 K the
    formula turns negative, which means rain, not negative snow.
    Works on scalars or arrays.
    """
    t = np.asarray(tmax_k, dtype=float)  # accept a single number, a list, or an array alike
    # np.where(condition, a, b) is an element-wise if/else: a where the condition is True, b elsewhere.
    # Above the reference temp the ratio falls twice as fast, so warm storms lose ratio quickly.
    slr = np.where(t > T_REF_K, 12.0 + 2.0 * (T_REF_K - t), 12.0 + (T_REF_K - t))
    slr = np.maximum(slr, 0.0)
    if floor is not None:
        slr = np.maximum(slr, floor)
    if cap is not None:
        slr = np.minimum(slr, cap)
    # ndim is 0 for a single value: give back a plain float in that case, an array otherwise.
    return slr if slr.ndim else float(slr)


def layer_tmax(temps_k: pd.DataFrame, bottom_hpa: int = 850, top_hpa: int = 500,
               psfc_hpa: pd.Series | None = None) -> pd.Series:
    """Max temperature across pressure levels in [top_hpa, bottom_hpa], ignoring levels below the model ground.

    temps_k: columns are pressure levels in hPa (ints), rows are time steps.
    psfc_hpa: model surface pressure per row. Levels with higher pressure than the surface are underground;
    models still fill them in by extending valley air downward (HRRR at Alta: +12 C at "850 mb"), which made
    Kuchera's warmest layer fake and the ratio negative. Without psfc_hpa every level in the range is used.
    """
    # Pressure falls with height, so 850 mb (~5,000 ft) is the BOTTOM and 500 mb (~18,000 ft) the TOP.
    # Python allows chained comparisons: a <= x <= b.
    cols = [c for c in temps_k.columns if top_hpa <= int(c) <= bottom_hpa]
    if not cols:
        raise ValueError(f"No pressure levels between {top_hpa} and {bottom_hpa} hPa in columns {list(temps_k.columns)}")
    t = temps_k[cols]
    if psfc_hpa is not None:
        # Boolean mask, True where a level is above ground (its pressure is lower than the surface's).
        above = pd.DataFrame({c: np.asarray(psfc_hpa, float) >= int(c) for c in cols}, index=t.index)
        t = t.where(above)  # underground values -> NaN, which max() skips
    return t.max(axis=1)  # axis=1 = across the columns, giving one max per row (per time step)


def qpf_mm_to_in(mm):  # QPF = quantitative precipitation forecast (liquid); models output millimeters
    return mm / 25.4
