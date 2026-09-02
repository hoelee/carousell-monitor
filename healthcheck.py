#!/usr/bin/env python3
"""Docker HEALTHCHECK: healthy iff a tick ran recently AND the last run was ok."""
import json
import os
import sys
import time

DATA_DIR = os.environ.get("DATA_DIR", "/data")
STALE = int(os.environ.get("HEALTH_STALE_SECONDS", "600"))

try:
    with open(os.path.join(DATA_DIR, "health.json"), encoding="utf-8") as f:
        h = json.load(f)
    age = time.time() - int(h.get("last_run_epoch", 0))
    if age <= STALE and h.get("ok") is True:
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
