"""
run.py
------
Development entry point for the Antarctic Navigation AI — ML Service.

Usage
-----
    # Development (auto-reload on file changes)
    python run.py

    # Production (single worker — TF is not thread-safe with default session)
    gunicorn -w 1 -b 0.0.0.0:8000 --timeout 120 app:app

Environment variables
---------------------
    FLASK_PORT     Port to listen on (default: 8000)
    FLASK_DEBUG    Set to "1" to enable Flask debug mode (default: 0)
    FLASK_HOST     Bind host (default: 0.0.0.0)
"""

import os
import logging
from app import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

if __name__ == "__main__":
    port  = int(os.environ.get("FLASK_PORT",  8000))
    host  = os.environ.get("FLASK_HOST",  "0.0.0.0")
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    print(f"""
╔══════════════════════════════════════════════════════╗
║       Antarctic Navigation AI — ML Service           ║
╠══════════════════════════════════════════════════════╣
║  Listening on : http://{host}:{port}              ║
║  Debug mode   : {"ON " if debug else "OFF"}                               ║
║                                                      ║
║  Endpoints:                                          ║
║    GET  /health                                      ║
║    GET  /model-info                                  ║
║    GET  /grid-info                                   ║
║    POST /predict                                     ║
╚══════════════════════════════════════════════════════╝
""")

    app.run(host=host, port=port, debug=debug)
