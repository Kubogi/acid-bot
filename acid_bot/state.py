from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .constants import LOGGER


@dataclass
class PersistentVoiceState:
    target_channel_id: int | None = None
    enabled: bool = False


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> PersistentVoiceState:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return PersistentVoiceState()
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Could not read state file %s: %s", self.path, exc)
            return PersistentVoiceState()

        if not isinstance(payload, dict):
            LOGGER.warning("Ignoring invalid state file %s: expected an object", self.path)
            return PersistentVoiceState()

        target = payload.get("target_channel_id")
        enabled = payload.get("enabled", False)
        if target is not None and (not isinstance(target, int) or isinstance(target, bool) or target <= 0):
            LOGGER.warning("Ignoring invalid target channel in %s", self.path)
            return PersistentVoiceState()
        if not isinstance(enabled, bool):
            LOGGER.warning("Ignoring invalid enabled flag in %s", self.path)
            return PersistentVoiceState()

        return PersistentVoiceState(target_channel_id=target, enabled=enabled)

    def save(self, state: PersistentVoiceState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        payload = {
            "target_channel_id": state.target_channel_id,
            "enabled": state.enabled,
        }

        with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, self.path)
