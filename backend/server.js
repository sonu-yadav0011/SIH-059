/**
 * server.js
 * ---------
 * Express backend for Antarctic Navigation AI.
 *
 * Responsibilities
 * ----------------
 * - Serve as the single HTTP gateway for the React frontend
 * - Proxy all ML inference requests to the Flask ML service
 * - Add request validation, error normalisation and logging
 *
 * Environment variables (see .env.example)
 * -----------------------------------------
 * PORT            Express listen port          (default: 5000)
 * ML_SERVICE_URL  Base URL of Flask ML service (default: http://localhost:8000)
 * NODE_ENV        "development" | "production"
 */

import express from "express";
import cors from "cors";
import axios from "axios";
import dotenv from "dotenv";

dotenv.config();

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const PORT           = parseInt(process.env.PORT          || "5000", 10);
const ML_SERVICE_URL = process.env.ML_SERVICE_URL          || "http://localhost:8000";
const NODE_ENV       = process.env.NODE_ENV                || "development";

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();

app.use(express.json({ limit: "50mb" }));  // large body — 7×66×57 float array
app.use(express.urlencoded({ extended: true }));

// CORS — allow Vite dev server (port 5173) and same-origin in production
const corsOrigins =
  NODE_ENV === "production"
    ? (process.env.FRONTEND_URL || false)
    : ["http://localhost:5173", "http://127.0.0.1:5173"];

app.use(cors({ origin: corsOrigins, credentials: true }));

// ---------------------------------------------------------------------------
// Tiny request logger
// ---------------------------------------------------------------------------

app.use((req, _res, next) => {
  console.log(`[${new Date().toISOString()}]  ${req.method}  ${req.path}`);
  next();
});

// ---------------------------------------------------------------------------
// ML service axios instance
// ---------------------------------------------------------------------------

const mlClient = axios.create({
  baseURL: ML_SERVICE_URL,
  timeout: 120_000,    // 2 min — TF inference can be slow on first call
  headers: { "Content-Type": "application/json" },
});

/** Forwards an axios error from the ML service as a JSON response. */
function handleMLError(err, res) {
  if (err.response) {
    // Flask returned an HTTP error
    return res.status(err.response.status).json({
      success: false,
      error:   err.response.data?.error || "ML service error",
      source:  "ml_service",
    });
  }
  if (err.code === "ECONNREFUSED" || err.code === "ECONNABORTED") {
    return res.status(503).json({
      success: false,
      error:   `ML service unavailable at ${ML_SERVICE_URL}. Is it running?`,
      source:  "express",
    });
  }
  console.error("[ML proxy error]", err.message);
  return res.status(500).json({
    success: false,
    error:   "Unexpected error communicating with ML service",
    source:  "express",
  });
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

/**
 * GET /api/health
 * ---------------
 * Returns combined health of Express + Flask ML service.
 *
 * Response
 * --------
 * {
 *   "express": "ok",
 *   "ml_service": "ok" | "unavailable",
 *   "ml_details": { model_loaded, uptime, timestamp },
 *   "timestamp": "..."
 * }
 */
app.get("/api/health", async (req, res) => {
  let mlStatus = "unavailable";
  let mlDetails = null;

  try {
    const { data } = await mlClient.get("/health");
    mlStatus  = data.status === "ok" ? "ok" : "degraded";
    mlDetails = data;
  } catch {
    // Flask not reachable — non-fatal for the health endpoint itself
  }

  res.json({
    express:    "ok",
    ml_service: mlStatus,
    ml_details: mlDetails,
    timestamp:  new Date().toISOString(),
  });
});

// ---------------------------------------------------------------------------

/**
 * GET /api/model-info
 * -------------------
 * Proxies Flask /model-info.  Returns the full model configuration JSON so
 * the frontend can display dataset/region/performance metadata.
 */
app.get("/api/model-info", async (_req, res) => {
  try {
    const { data } = await mlClient.get("/model-info");
    res.json(data);
  } catch (err) {
    handleMLError(err, res);
  }
});

// ---------------------------------------------------------------------------

/**
 * GET /api/grid-info
 * ------------------
 * Proxies Flask /grid-info.  Returns lat/lon coordinate grids and the
 * spatial mask so the frontend map can be initialised before any prediction.
 */
app.get("/api/grid-info", async (_req, res) => {
  try {
    const { data } = await mlClient.get("/grid-info");
    res.json(data);
  } catch (err) {
    handleMLError(err, res);
  }
});

// ---------------------------------------------------------------------------

/**
 * POST /api/predict
 * -----------------
 * Runs the CNN sea-ice forecast.
 *
 * Request body
 * ------------
 * Option A (auto-load from NetCDF — recommended):
 *   { "target_date": "2024-06-01" }
 *
 * Option B (manual data):
 *   {
 *     "target_date": "2024-06-01",
 *     "last_7_days": [[[...66×57 floats...]]]  // shape (7, 66, 57)
 *   }
 *
 * Response
 * --------
 * Same as Flask /predict response (see sea_ice_model.py for full schema).
 */
app.post("/api/predict", async (req, res) => {
  const { target_date, last_7_days } = req.body;

  // Express-level validation
  if (!target_date) {
    return res.status(400).json({
      success: false,
      error:   "'target_date' is required (ISO format, e.g. '2024-06-01')",
    });
  }

  if (!/^\d{4}-\d{2}-\d{2}$/.test(target_date)) {
    return res.status(400).json({
      success: false,
      error:   `Invalid date format: '${target_date}'. Expected YYYY-MM-DD.`,
    });
  }

  try {
    const payload = { target_date };
    if (last_7_days !== undefined) payload.last_7_days = last_7_days;

    const { data } = await mlClient.post("/predict", payload);
    res.json(data);
  } catch (err) {
    handleMLError(err, res);
  }
});

// ---------------------------------------------------------------------------

/**
 * GET /api/available-dates
 * ------------------------
 * Returns the date range available in the NetCDF dataset so the frontend
 * can constrain its date picker.
 *
 * Calls Flask /predict with a sentinel body to trigger data_loader.get_available_date_range().
 * We implement this directly in Express by calling a lightweight Flask endpoint.
 */
app.get("/api/available-dates", async (_req, res) => {
  // We call the Flask /model-info endpoint and augment it with dataset date info.
  // The date range is also derivable from model_config["dataset"] but the
  // data_loader can provide exact min/max dates.
  try {
    // Attempt to call a dedicated /available-dates endpoint on Flask
    const { data } = await mlClient.get("/available-dates");
    res.json(data);
  } catch (err) {
    if (err.response?.status === 404) {
      // Flask doesn't have that endpoint — return a static range from model config
      res.json({
        success: true,
        start:   "2023-01-01",
        end:     "2025-12-31",
        note:    "Static range from model config — install the full Flask /available-dates route for live range.",
      });
    } else {
      handleMLError(err, res);
    }
  }
});

// ---------------------------------------------------------------------------
// Fallthrough
// ---------------------------------------------------------------------------

app.use((_req, res) => {
  res.status(404).json({ success: false, error: "Endpoint not found." });
});

app.use((err, _req, res, _next) => {
  console.error("[Express error]", err);
  res.status(500).json({ success: false, error: "Internal server error." });
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

app.listen(PORT, () => {
  console.log(`
╔══════════════════════════════════════════════════════╗
║     Antarctic Navigation AI — Express Backend        ║
╠══════════════════════════════════════════════════════╣
║  Listening on  : http://localhost:${PORT}              ║
║  ML Service    : ${ML_SERVICE_URL}     ║
║  Environment   : ${NODE_ENV.padEnd(12)}                     ║
║                                                      ║
║  API Endpoints:                                      ║
║    GET  /api/health                                  ║
║    GET  /api/model-info                              ║
║    GET  /api/grid-info                               ║
║    GET  /api/available-dates                         ║
║    POST /api/predict                                 ║
╚══════════════════════════════════════════════════════╝
`);
});

export default app;
