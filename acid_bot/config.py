from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .constants import BASE_DIR, DEFAULT_ROTATION_SECONDS, DEFAULT_STATE_PATH


class ConfigurationError(ValueError):
    """Raised when required environment configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    token: str
    voice_tokens: tuple[str, ...]
    rotation_seconds: float
    guild_id: int
    authorized_user_ids: frozenset[int]
    state_path: Path = DEFAULT_STATE_PATH

    @classmethod
    def from_env(cls, state_path: Path = DEFAULT_STATE_PATH) -> "Config":
        load_dotenv(BASE_DIR / ".env")

        token = os.getenv("DISCORD_TOKEN", "").strip()
        raw_voice_tokens = os.getenv("VOICE_BOT_TOKENS", "").strip()
        raw_rotation_seconds = os.getenv("VOICE_ROTATION_SECONDS", "").strip()
        raw_guild_id = os.getenv("GUILD_ID", "").strip()
        raw_authorized_user_ids = os.getenv("AUTHORIZED_USER_IDS", "").strip()

        if not token:
            raise ConfigurationError("DISCORD_TOKEN is missing. Add it to .env.")
        if not raw_guild_id:
            raise ConfigurationError("GUILD_ID is missing. Add it to .env.")
        if not raw_authorized_user_ids:
            raise ConfigurationError("AUTHORIZED_USER_IDS is missing. Add it to .env.")

        try:
            guild_id = int(raw_guild_id)
        except ValueError as exc:
            raise ConfigurationError("GUILD_ID must be a positive integer.") from exc

        if guild_id <= 0:
            raise ConfigurationError("GUILD_ID must be a positive integer.")

        authorized_user_ids: set[int] = set()
        for raw_user_id in raw_authorized_user_ids.split(","):
            value = raw_user_id.strip()
            if not value:
                raise ConfigurationError(
                    "AUTHORIZED_USER_IDS must be a comma-separated list of positive integers."
                )
            try:
                user_id = int(value)
            except ValueError as exc:
                raise ConfigurationError(
                    "AUTHORIZED_USER_IDS must be a comma-separated list of positive integers."
                ) from exc
            if user_id <= 0:
                raise ConfigurationError(
                    "AUTHORIZED_USER_IDS must be a comma-separated list of positive integers."
                )
            authorized_user_ids.add(user_id)

        voice_tokens: list[str] = []
        for raw_voice_token in raw_voice_tokens.split(","):
            value = raw_voice_token.strip()
            if value and value != token and value not in voice_tokens:
                voice_tokens.append(value)

        if not raw_rotation_seconds:
            rotation_seconds = DEFAULT_ROTATION_SECONDS
        else:
            try:
                rotation_seconds = float(raw_rotation_seconds)
            except ValueError as exc:
                raise ConfigurationError(
                    "VOICE_ROTATION_SECONDS must be a positive number of seconds."
                ) from exc
            if not math.isfinite(rotation_seconds) or rotation_seconds <= 0:
                raise ConfigurationError(
                    "VOICE_ROTATION_SECONDS must be a positive number of seconds."
                )

        return cls(
            token=token,
            voice_tokens=tuple(voice_tokens),
            rotation_seconds=rotation_seconds,
            guild_id=guild_id,
            authorized_user_ids=frozenset(authorized_user_ids),
            state_path=state_path,
        )
