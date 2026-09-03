"""
data_loader.py
--------------
Utility to fetch the 7-day sea-ice concentration history that the CNN needs
from the local NetCDF dataset:

    data/processed/sea_ice_aoi/sea_ice_bharati_2023_2025.nc

This means the Express backend (and the Flask `/predict` endpoint) only needs
to send a single `target_date` — the service auto-fills the history.

Variable expected inside the .nc file
--------------------------------------
The notebook 01_inspect_sea_ice.ipynb stores the dataset produced by
NSIDC-0803 (AMSR2).  We expect a data variable whose name contains "siconc"
or "sea_ice" with dimensions (time, y, x) or (time, latitude, longitude).

Coordinate names are detected automatically (lat/lon or y/x fallback).
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataset path
# ---------------------------------------------------------------------------

BASE_DIR    = Path(__file__).resolve().parent.parent          # repo root
NC_PATH     = (
    BASE_DIR
    / "data"
    / "processed"
    / "sea_ice_aoi"
    / "sea_ice_bharati_2023_2025.nc"
)

# Expected spatial grid dimensions (must match model training config)
GRID_HEIGHT = 66
GRID_WIDTH  = 57

# ---------------------------------------------------------------------------
# Internal: lazy dataset cache
# ---------------------------------------------------------------------------

_ds_cache: "xr.Dataset | None" = None   # noqa: F821


def _open_dataset():
    """Open (and cache) the NetCDF dataset with xarray."""
    global _ds_cache
    if _ds_cache is not None:
        return _ds_cache

    try:
        import xarray as xr
    except ImportError as exc:
        raise ImportError("xarray is required for data_loader. "
                          "Run: pip install xarray netCDF4") from exc

    if not NC_PATH.exists():
        raise FileNotFoundError(
            f"NetCDF file not found: {NC_PATH}\n"
            "Download the dataset using the NSIDC download script in notebooks/."
        )

    logger.info("Opening NetCDF dataset: %s", NC_PATH)
    ds = xr.open_dataset(NC_PATH, engine="netcdf4")
    logger.info("Dataset opened.  Variables: %s", list(ds.data_vars))
    _ds_cache = ds
    return ds


def _find_sic_variable(ds) -> str:
    """Return the name of the sea-ice concentration variable in *ds*."""
    candidates = []
    for var in ds.data_vars:
        vl = var.lower()
        if any(k in vl for k in ("siconc", "sic", "sea_ice", "ice_conc", "concentration")):
            candidates.append(var)

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # prefer shorter / more specific name
        return sorted(candidates, key=len)[0]

    raise KeyError(
        f"Cannot identify sea-ice concentration variable in dataset. "
        f"Available variables: {list(ds.data_vars)}"
    )


def _find_time_dim(da) -> str:
    """Return the name of the time dimension in DataArray *da*."""
    for dim in da.dims:
        if "time" in dim.lower():
            return dim
    raise KeyError(f"No 'time' dimension found in DataArray dims: {list(da.dims)}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_available_date_range() -> dict:
    """Return the min/max dates available in the NetCDF file."""
    ds  = _open_dataset()
    var = _find_sic_variable(ds)
    da  = ds[var]
    t   = _find_time_dim(da)

    times = da[t].values
    return {
        "start": str(np.datetime64(times.min(), "D")),
        "end":   str(np.datetime64(times.max(), "D")),
    }


def get_last_7_days(target_date: str) -> np.ndarray:
    """
    Retrieve the 7 daily SIC grids *immediately before* target_date.

    Parameters
    ----------
    target_date : str
        ISO date string, e.g. "2024-06-01".  The 7 days returned are
        target_date-7 … target_date-1 (inclusive).

    Returns
    -------
    np.ndarray of shape (7, GRID_HEIGHT, GRID_WIDTH), dtype float32
        Values in [0, 1]; NaN for ocean / missing cells.

    Raises
    ------
    ValueError
        If fewer than 7 days of history are available before target_date.
    """
    ds  = _open_dataset()
    var = _find_sic_variable(ds)
    da  = ds[var]
    t   = _find_time_dim(da)

    # Build list of 7 requested dates
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    dates = [
        (target_dt - timedelta(days=7 - i)).strftime("%Y-%m-%d")
        for i in range(7)
    ]

    frames = []
    missing = []
    for d in dates:
        try:
            frame = (
                da.sel({t: np.datetime64(d)}, method="nearest")
                  .values
                  .astype(np.float32)
            )
            # Normalise: if values are in 0-100 range, scale to 0-1
            if np.nanmax(frame) > 1.5:
                frame = frame / 100.0
            frame = np.clip(frame, 0.0, 1.0)

            # Ensure correct spatial shape
            if frame.shape != (GRID_HEIGHT, GRID_WIDTH):
                raise ValueError(
                    f"Grid shape mismatch on {d}: "
                    f"expected ({GRID_HEIGHT}, {GRID_WIDTH}), got {frame.shape}"
                )
            frames.append(frame)
        except KeyError:
            logger.warning("Date %s not found in dataset, filling with NaN.", d)
            missing.append(d)
            frames.append(np.full((GRID_HEIGHT, GRID_WIDTH), np.nan, dtype=np.float32))

    if len(missing) > 3:
        raise ValueError(
            f"Too many missing dates ({len(missing)}/7) around {target_date}. "
            f"Missing: {missing}"
        )

    result = np.stack(frames, axis=0)   # (7, H, W)
    logger.debug("Loaded 7-day history for %s — shape %s", target_date, result.shape)
    return result


def get_grid_metadata() -> dict:
    """
    Return lat/lon coordinate arrays from the dataset as Python lists.
    Used by the `/grid-info` endpoint.
    """
    ds  = _open_dataset()
    var = _find_sic_variable(ds)
    da  = ds[var]

    # Try to get coordinate arrays from dataset coords
    lat_names = ("latitude", "lat", "y", "nav_lat")
    lon_names = ("longitude", "lon", "x", "nav_lon")

    lat_arr = lon_arr = None
    for name in lat_names:
        if name in ds.coords:
            lat_arr = ds.coords[name].values
            break
    for name in lon_names:
        if name in ds.coords:
            lon_arr = ds.coords[name].values
            break

    # Fall back to saved artifact grids
    from sea_ice_model import _load_resources  # noqa: E402
    res = _load_resources()

    if lat_arr is None:
        lat_arr = res["latitude_grid"]
    if lon_arr is None:
        lon_arr = res["longitude_grid"]

    # Ensure 2-D (H, W)
    if lat_arr.ndim == 1:
        lon_arr, lat_arr = np.meshgrid(lon_arr, lat_arr)

    return {
        "latitude_grid":  lat_arr.tolist(),
        "longitude_grid": lon_arr.tolist(),
        "shape": {
            "height": GRID_HEIGHT,
            "width":  GRID_WIDTH,
        },
    }
