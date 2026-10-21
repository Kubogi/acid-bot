from __future__ import annotations

import logging
import os
import platform

import discord

from .client import VoiceBot
from .config import Config, ConfigurationError
from .constants import LOGGER


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    LOGGER.info(
        "Starting acid-bot diagnostics: pid=%s pm_id=%s python=%s discord_py=%s "
        "platform=%s",
        os.getpid(),
        os.getenv("pm_id", "not-running-under-pm2"),
        platform.python_version(),
        discord.__version__,
        platform.platform(),
    )

    try:
        config = Config.from_env()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    bot = VoiceBot(config)
    bot.run(config.token, log_handler=None)
