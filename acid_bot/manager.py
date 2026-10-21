from __future__ import annotations

import asyncio
import time
from typing import Any

import discord

from .constants import DEFAULT_ROTATION_SECONDS, LOGGER
from .diagnostics import DiagnosticVoiceClient, gateway_latency_for, voice_latency_for
from .state import StateStore


class VoiceOperationError(RuntimeError):
    """Raised when a requested voice operation cannot be completed."""


class RetryBackoff:
    def __init__(self, minimum: float = 5.0, maximum: float = 60.0):
        self.minimum = minimum
        self.maximum = maximum
        self.current = minimum

    def next_delay(self) -> float:
        delay = self.current
        self.current = min(self.current * 2, self.maximum)
        return delay

    def reset(self) -> None:
        self.current = self.minimum


class VoiceManager:
    def __init__(
        self,
        bot: discord.Client,
        guild_id: int,
        store: StateStore,
        rotation_seconds: float = DEFAULT_ROTATION_SECONDS,
    ):
        self.bot = bot
        self.guild_id = guild_id
        self.store = store
        self.state = store.load()
        self.rotation_seconds = rotation_seconds
        self.last_error: str | None = None
        self.started_at = time.monotonic()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._watchdog_task: asyncio.Task[None] | None = None
        self._backoff = RetryBackoff()
        self._connection_attempts = 0
        self._pool: list[discord.Client] = [bot]
        self._active_index: int | None = None
        self._seat_since: float | None = None

    def set_pool(self, clients: list[discord.Client]) -> None:
        if not clients:
            raise ValueError("The voice seat pool requires at least one client.")
        self._pool = list(clients)
        self._active_index = None
        self._seat_since = None

    def start(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(
                self._watchdog(), name="voice-connection-watchdog"
            )

    def wake(self) -> None:
        self._wake.set()

    async def connect_to(self, channel: discord.VoiceChannel) -> None:
        async with self._lock:
            self.state.target_channel_id = channel.id
            self.state.enabled = True
            self.store.save(self.state)
            try:
                await self._ensure_seat_locked(channel)
            except Exception as exc:
                self._remember_error(exc)
                raise
            else:
                self._mark_success()
        self.wake()

    async def leave(self) -> None:
        async with self._lock:
            self.state.enabled = False
            self.store.save(self.state)
            voice_client = self._active_voice_client()
            if voice_client is not None:
                await voice_client.disconnect(force=True)
            self._active_index = None
            self._seat_since = None
            self._mark_success()
        self.wake()

    async def rejoin(self) -> discord.VoiceChannel:
        async with self._lock:
            channel = self._resolve_target_locked()
            self.state.enabled = True
            self.store.save(self.state)
            try:
                await self._rotate_locked(channel, reason="manual rejoin")
            except Exception as exc:
                self._remember_error(exc)
                raise
            else:
                self._mark_success()
        self.wake()
        return channel

    async def ensure_connected(self) -> None:
        async with self._lock:
            if not self.state.enabled:
                return
            channel = self._resolve_target_locked()
            await self._ensure_seat_locked(channel)
            self._mark_success()

    async def shutdown(self) -> None:
        task = self._watchdog_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            voice_client = self._active_voice_client()
            if voice_client is not None:
                try:
                    await voice_client.disconnect(force=True)
                except discord.DiscordException:
                    LOGGER.exception("Failed to disconnect cleanly during shutdown")
            self._active_index = None
            self._seat_since = None

    def status(self) -> dict[str, Any]:
        voice_client = self._active_voice_client()
        connected = bool(voice_client is not None and voice_client.is_connected())
        actual_channel = voice_client.channel if connected else None
        now = time.monotonic()
        seat_age = int(now - self._seat_since) if self._seat_since is not None else None
        next_rotation = None
        if connected and self._seat_since is not None and len(self._pool) > 1:
            next_rotation = max(0, int(self.rotation_seconds - (now - self._seat_since)))
        return {
            "enabled": self.state.enabled,
            "target_channel_id": self.state.target_channel_id,
            "connected": connected,
            "actual_channel": actual_channel,
            "uptime_seconds": int(now - self.started_at),
            "last_error": self.last_error,
            "pool_size": len(self._pool),
            "active_seat": self._pool_label(self._active_index) if connected else None,
            "seat_age_seconds": seat_age if connected else None,
            "next_rotation_seconds": next_rotation,
        }

    def _resolve_target_locked(self) -> discord.VoiceChannel:
        if self.state.target_channel_id is None:
            raise VoiceOperationError("No target channel has been saved. Use /voice join or /voice move first.")

        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            raise VoiceOperationError("The configured Discord server is currently unavailable.")

        channel = guild.get_channel(self.state.target_channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise VoiceOperationError(
                f"Voice channel {self.state.target_channel_id} no longer exists or is unavailable."
            )
        return channel

    def _pool_label(self, index: int | None) -> str:
        if index is None or index < 0 or index >= len(self._pool):
            return "none"
        client = self._pool[index]
        user = getattr(client, "user", None)
        name = str(user) if user is not None else "not-logged-in"
        role = "main" if index == 0 else "secondary"
        return f"{role}[{index}] {name}"

    def _active_voice_client(self) -> discord.VoiceClient | None:
        if self._active_index is None or self._active_index >= len(self._pool):
            return None
        guild = self._pool[self._active_index].get_guild(self.guild_id)
        if guild is None:
            return None
        voice_client = guild.voice_client
        return voice_client if isinstance(voice_client, discord.VoiceClient) else None

    def _rotation_due(self) -> bool:
        return (
            len(self._pool) > 1
            and self._seat_since is not None
            and time.monotonic() - self._seat_since >= self.rotation_seconds
        )

    async def _ensure_seat_locked(self, channel: discord.VoiceChannel) -> None:
        voice_client = self._active_voice_client()
        if voice_client is not None and voice_client.is_connected():
            if getattr(voice_client.channel, "id", None) != channel.id:
                guild = self._pool[self._active_index].get_guild(self.guild_id)
                seat_channel = guild.get_channel(channel.id) if guild is not None else None
                if not isinstance(seat_channel, discord.VoiceChannel):
                    raise VoiceOperationError(
                        f"Voice channel {channel.id} is unavailable to the seated pool member."
                    )
                LOGGER.info("Moving to voice channel %s (%s)", seat_channel.name, seat_channel.id)
                await voice_client.move_to(seat_channel)
            elif self._rotation_due():
                await self._rotate_locked(channel, reason="scheduled rotation")
            return
        await self._rotate_locked(channel, reason="seat disconnected")

    async def _rotate_locked(self, channel: discord.VoiceChannel, reason: str) -> None:
        start = self._active_index if self._active_index is not None else -1
        failures: list[str] = []
        for step in range(len(self._pool)):
            index = (start + 1 + step) % len(self._pool)
            label = self._pool_label(index)
            try:
                await self._seat_locked(index, channel)
            except Exception as exc:
                failures.append(f"{label}: {exc}")
                LOGGER.warning(
                    "Voice seat handover failed: seat=%s reason=%s error=%s",
                    label,
                    reason,
                    exc,
                )
            else:
                if index != start:
                    LOGGER.info(
                        "Voice seat rotated: previous=%s current=%s reason=%s",
                        self._pool_label(start),
                        label,
                        reason,
                    )
                return
        raise VoiceOperationError(
            f"No pool member could hold the voice seat ({reason}). " + "; ".join(failures)
        )

    async def _seat_locked(
        self, index: int, channel: discord.VoiceChannel
    ) -> discord.VoiceClient:
        client = self._pool[index]
        label = self._pool_label(index)
        if client.is_closed() or getattr(client, "_acid_login_failed", False):
            raise VoiceOperationError(f"Pool member {label} is unavailable.")
        if not client.is_ready():
            try:
                await asyncio.wait_for(client.wait_until_ready(), timeout=15.0)
            except TimeoutError as exc:
                raise VoiceOperationError(
                    f"Pool member {label} is not logged in and ready."
                ) from exc

        guild = client.get_guild(self.guild_id)
        if guild is None:
            raise VoiceOperationError(
                f"Pool member {label} cannot see the configured Discord server."
            )

        seat_channel = guild.get_channel(channel.id)
        if not isinstance(seat_channel, discord.VoiceChannel):
            raise VoiceOperationError(
                f"Pool member {label} cannot see voice channel {channel.id}."
            )

        voice_client = guild.voice_client
        if voice_client is not None and voice_client.is_connected():
            if getattr(voice_client.channel, "id", None) == seat_channel.id:
                seated = voice_client
            else:
                LOGGER.info(
                    "Moving to voice channel %s (%s)", seat_channel.name, seat_channel.id
                )
                await voice_client.move_to(seat_channel)
                seated = voice_client
        else:
            if voice_client is not None:
                connection_state = getattr(
                    getattr(voice_client, "_connection", None), "state", None
                )
                last_close_code = getattr(voice_client, "last_voice_close_code", None)
                last_close_at = getattr(voice_client, "last_voice_close_at", None)
                close_age = (
                    f"{time.monotonic() - last_close_at:.1f}s"
                    if last_close_at is not None
                    else "unavailable"
                )
                LOGGER.warning(
                    "Replacing existing disconnected voice client: seat=%s client_id=%s "
                    "flow_state=%s current_channel_id=%s target_channel_id=%s "
                    "last_close_code=%s last_close_age=%s (an in-progress library "
                    "reconnect may be racing recovery)",
                    label,
                    id(voice_client),
                    getattr(connection_state, "name", "unknown"),
                    getattr(getattr(voice_client, "channel", None), "id", None),
                    channel.id,
                    last_close_code,
                    close_age,
                )
                await voice_client.disconnect(force=True)

            self._connection_attempts += 1
            attempt = self._connection_attempts
            started_at = time.monotonic()
            LOGGER.info(
                "Starting managed voice connection: attempt=%s seat=%s channel=%s channel_id=%s",
                attempt,
                label,
                seat_channel.name,
                seat_channel.id,
            )
            seated = await seat_channel.connect(
                timeout=30.0,
                reconnect=True,
                self_deaf=True,
                cls=DiagnosticVoiceClient,
            )
            LOGGER.info(
                "Managed voice connection succeeded: attempt=%s seat=%s elapsed=%.2fs "
                "client_id=%s endpoint=%s voice_latency=%s gateway_latency=%s",
                attempt,
                label,
                time.monotonic() - started_at,
                id(seated),
                getattr(seated, "endpoint", None),
                voice_latency_for(seated),
                gateway_latency_for(seated),
            )

        previous = self._active_voice_client()
        if previous is not None and previous is not seated:
            try:
                await previous.disconnect(force=True)
            except discord.DiscordException:
                LOGGER.exception("Failed to release the previous voice seat cleanly")

        self._active_index = index
        self._seat_since = time.monotonic()
        return seated

    async def _watchdog(self) -> None:
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            wait_seconds = 30.0
            if self.state.enabled:
                try:
                    await self.ensure_connected()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._remember_error(exc)
                    wait_seconds = self._backoff.next_delay()
                    LOGGER.warning(
                        "Voice connection unavailable; retrying in %.0f seconds: %s",
                        wait_seconds,
                        exc,
                    )
                else:
                    wait_seconds = 15.0

            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait_seconds)
            except TimeoutError:
                pass
            finally:
                self._wake.clear()

    def _remember_error(self, exc: Exception) -> None:
        message = str(exc).strip()
        self.last_error = message or exc.__class__.__name__

    def _mark_success(self) -> None:
        self.last_error = None
        self._backoff.reset()
