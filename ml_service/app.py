"""
app.py
------
Flask REST API for the Antarctic Navigation AI — ML Service.

Endpoints
---------
GET  /health          Health check (model load status + uptime).
GET  /model-info      Full model configuration JSON.
GET  /grid-info       Lat/lon coordinate grids + spatial mask.
POST /predict         Run the CNN forecast.

Running
-------
Development:   python run.py
Production:    gunicorn -w 1 -b 0.0.0.0:8000 app:app

The Express backend talks to this service via the ML_SERVICE_URL env var
(default http://localhost:8000).
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Allow cross-origin requests from the Vite dev server and the Express proxy
CORS(app, resources={r"/*": {"origins": "*"}})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_START_TIME = time.time()

# ---------------------------------------------------------------------------
# Lazy imports (keep startup fast; TF takes several seconds to load)
# ---------------------------------------------------------------------------

def _get_model_module():
    import sea_ice_model  # noqa: PLC0415
    return sea_ice_model


def _get_data_loader():
    import data_loader  # noqa: PLC0415
    return data_loader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error(message: str, status: int = 400) -> tuple:
    return jsonify({"success": False, "error": message}), status


def _uptime() -> str:
    seconds = int(time.time() - _START_TIME)
    h, rem  = divmod(seconds, 3600)
    m, s    = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    """
    Returns the service status and whether the model is already loaded.

    Response
    --------
    {
        "status": "ok",
        "model_loaded": true | false,
        "uptime": "00:01:23",
        "timestamp": "2024-06-01T12:00:00"
    }
    """
    sm = _get_model_module()
    return jsonify({
        "status":       "ok",
        "service":      "antarctic-ml-service",
        "model_loaded": sm.is_loaded(),
        "uptime":       _uptime(),
        "timestamp":    datetime.utcnow().isoformat() + "Z",
    })


@app.route("/model-info", methods=["GET"])
def model_info():
    """
    Returns the full model_config.json so the Express backend / frontend
    can display metadata without loading the heavy model weights.

    Response
    --------
    {
        "success": true,
        "config": { ... model_config.json contents ... }
    }
    """
    try:
        sm  = _get_model_module()
        cfg = sm.get_model_config()
        return jsonify({"success": True, "config": cfg})
    except FileNotFoundError as exc:
        return _error(str(exc), 503)
    except Exception as exc:
        logger.exception("Error fetching model info")
        return _error(f"Internal error: {exc}", 500)


@app.route("/grid-info", methods=["GET"])
def grid_info():
    """
    Returns the spatial coordinate grids and the ocean mask.

    Response
    --------
    {
        "success": true,
        "latitude_grid": [[...]],
        "longitude_grid": [[...]],
        "mask": [[...]],
        "shape": { "height": 66, "width": 57 }
    }
    """
    try:
        sm  = _get_model_module()
        res = sm._load_resources()

        return jsonify({
            "success":        True,
            "latitude_grid":  res["latitude_grid"].tolist(),
            "longitude_grid": res["longitude_grid"].tolist(),
            "mask":           res["spatial_mask"].tolist(),
            "shape": {
                "height": res["latitude_grid"].shape[0],
                "width":  res["latitude_grid"].shape[1],
            },
        })
    except FileNotFoundError as exc:
        return _error(str(exc), 503)
    except Exception as exc:
        logger.exception("Error in /grid-info")
        return _error(f"Internal error: {exc}", 500)


@app.route("/predict", methods=["POST"])
def predict():
    """
    Run the sea-ice CNN forecast.

    Request body (JSON)
    -------------------
    Option A — auto-load history from the NetCDF dataset (recommended):
    {
        "target_date": "2024-06-01"
    }

    Option B — provide the 7-day history explicitly:
    {
        "target_date": "2024-06-01",
        "last_7_days": [[[...66 x 57 floats...]], ...]   // shape (7, 66, 57)
    }

    Response
    --------
    {
        "success": true,
        "target_date": "2024-06-01",
        "prediction": [[...66 x 57 floats, NaN→null...]],
        "latitude_grid": [[...]],
        "longitude_grid": [[...]],
        "mask": [[...]],
        "stats": {
            "mean_concentration": 0.42,
            "min_concentration":  0.0,
            "max_concentration":  1.0,
            "ice_coverage_fraction": 0.61,
            "valid_cells": 2108
        },
        "model_info": { ... }
    }
    """
    body = request.get_json(silent=True)

    if not body:
        return _error("Request body must be JSON.", 400)

    # ---- target_date (required) ----
    target_date = body.get("target_date")
    if not target_date:
        return _error("'target_date' is required (ISO format, e.g. '2024-06-01').", 400)

    # Validate date format
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return _error(
            f"Invalid date format: '{target_date}'. Expected YYYY-MM-DD.", 400
        )

    # ---- last_7_days — auto-load or user-supplied ----
    sm = _get_model_module()
    last_7_days = body.get("last_7_days")

    if last_7_days is None:
        # Option A: auto-load from NetCDF
        try:
            dl = _get_data_loader()
            logger.info("Auto-loading 7-day SIC history for %s …", target_date)
            import numpy as np
            last_7_days = dl.get_last_7_days(target_date)
        except FileNotFoundError as exc:
            return _error(
                f"NetCDF dataset not found — cannot auto-load history. "
                f"Provide 'last_7_days' in the request body, or fix the path.\n{exc}",
                503,
            )
        except ValueError as exc:
            return _error(str(exc), 400)
        except Exception as exc:
            logger.exception("Failed to auto-load SIC history")
            return _error(f"Data loading error: {exc}", 500)
    else:
        # Option B: user-supplied array — basic shape check
        import numpy as np
        try:
            last_7_days = np.asarray(last_7_days, dtype=np.float32)
        except Exception:
            return _error("'last_7_days' must be a numeric 3-D array.", 400)

        if last_7_days.shape != (7, 66, 57):
            return _error(
                f"'last_7_days' must have shape (7, 66, 57), got {list(last_7_days.shape)}.",
                400,
            )

    # ---- Run inference ----
    try:
        logger.info("Running CNN inference for %s …", target_date)
        t0     = time.time()
        result = sm.predict_sea_ice(last_7_days, target_date)
        elapsed = round(time.time() - t0, 3)
        result["inference_time_seconds"] = elapsed
        logger.info("Inference completed in %.3f s", elapsed)
        return jsonify(result)

    except ValueError as exc:
        return _error(str(exc), 400)
    except FileNotFoundError as exc:
        return _error(str(exc), 503)
    except Exception as exc:
        logger.exception("Inference error")
        return _error(f"Inference failed: {exc}", 500)


@app.route("/available-dates", methods=["GET"])
def available_dates():
    """
    Returns the min/max dates available in the NetCDF dataset.

    Response
    --------
    {
        "success": true,
        "start": "2023-01-01",
        "end":   "2025-12-31"
    }
    """
    try:
        dl    = _get_data_loader()
        dates = dl.get_available_date_range()
        return jsonify({"success": True, **dates})
    except FileNotFoundError as exc:
        return _error(str(exc), 503)
    except Exception as exc:
        logger.exception("Error in /available-dates")
        return _error(f"Internal error: {exc}", 500)


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(_):
    return _error("Endpoint not found.", 404)


@app.errorhandler(405)
def method_not_allowed(_):
    return _error("Method not allowed.", 405)


@app.errorhandler(500)
def internal_error(exc):
    logger.exception("Unhandled exception")
    return _error(f"Internal server error: {exc}", 500)
