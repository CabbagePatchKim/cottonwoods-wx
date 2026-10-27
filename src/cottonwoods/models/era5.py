"""ERA5 pressure-level pulls for flow-regime labels, via the Copernicus CDS API.

UNTESTED, and needs a free CDS account + ~/.cdsapirc. Dataset/variable names below follow the CDS catalogue
(reanalysis-era5-pressure-levels); confirm in the CDS web form ("Show API request") if a call is rejected.
"""
from __future__ import annotations

from pathlib import Path


def request_pressure_levels(
    out_path: str | Path,
    dates: list[str],          # ["2024-11-23", "2024-11-24", ...]
    area: tuple[float, float, float, float] = (42.0, -113.0, 39.0, -110.0),  # N, W, S, E
    levels: tuple[str, ...] = ("700", "500", "250"),
    hours: tuple[str, ...] = tuple(f"{h:02d}:00" for h in range(0, 24, 3)),
    variables: tuple[str, ...] = ("temperature", "u_component_of_wind", "v_component_of_wind",
                                  "specific_humidity", "geopotential"),
) -> Path:
    import cdsapi  # imported here, not at the top, so the rest of the package works without it installed

    # The CDS form takes separate year/month/day lists and returns every combination, so a request spanning
    # two months can include a few extra days. sorted({...}) = unique values in order (a set drops repeats).
    years = sorted({d[:4] for d in dates})
    months = sorted({d[5:7] for d in dates})
    days = sorted({d[8:10] for d in dates})
    c = cdsapi.Client()
    c.retrieve(
        "reanalysis-era5-pressure-levels",
        {
            "product_type": "reanalysis",
            "variable": list(variables),
            "pressure_level": list(levels),
            "year": years,
            "month": months,
            "day": days,
            "time": list(hours),
            "area": list(area),
            "format": "netcdf",
        },
        str(out_path),
    )
    return Path(out_path)
