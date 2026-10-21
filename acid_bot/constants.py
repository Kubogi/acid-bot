from __future__ import annotations

import logging
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
BASE_DIR = PACKAGE_DIR.parent
DEFAULT_STATE_PATH = BASE_DIR / "data" / "voice_state.json"
DEFAULT_ROTATION_SECONDS = 10_800.0  # 3 hours
LOGGER = logging.getLogger("acid_bot")
