from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import platform
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.errors import ConnectionClosed
from discord.ext import commands
from discord.gateway import DiscordVoiceWebSocket
from discord.voice_state import ConnectionFlowState, VoiceConnectionState
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = BASE_DIR / "data" / "voice_state.json"
LOGGER = logging.getLogger("acid_bot")


VOICE_CLOSE_CAUSES = {
    1000: "normal closure",
    4006: "voice session is no longer valid",
    4009: "voice session timed out",
    4011: "voice server was not found",
    4014: "Discord disconnected this client (kick, move, or main gateway loss)",
    4015: "Discord voice server crashed",
    4017: "the channel requires E2EE/DAVE support",
    4021: "voice connection was rate limited",
    4022: "call terminated (channel deletion or voice server change)",
}


def describe_voice_close(code: int) -> str:
    return VOICE_CLOSE_CAUSES.get(code, "unknown or undocumented voice close reason")


def format_latency(value: float | None) -> str:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "unavailable"
    return f"{value * 1000:.0f}ms"


def gateway_latency_for(client: discord.VoiceClient) -> str:
    try:
        websocket = client._state._get_websocket(client.guild.id)
        return format_latency(websocket.latency)
    except Exception:
        return "unavailable"


def voice_latency_for(client: discord.VoiceClient) -> str:
    try:
        return format_latency(client.latency)
    except Exception:
        return "unavailable"


class DiagnosticVoiceWebSocket(DiscordVoiceWebSocket):
    """Voice WebSocket that records close codes without logging sensitive payloads."""

    async def poll_event(self) -> None:
        try:
            await super().poll_event()
        except ConnectionClosed as exc:
            state = self._connection
            client = state.voice_client
            client.last_voice_close_code = exc.code
            client.last_voice_close_at = time.monotonic()
            LOGGER.warning(
                "Voice websocket closed: code=%s cause=%s flow_state=%s "
                "guild_id=%s channel_id=%s endpoint=%s voice_latency=%s gateway_latency=%s",
                exc.code,
                describe_voice_close(exc.code),
                state.state.name,
                client.guild.id,
                client.channel.id,
                state.endpoint,
                voice_latency_for(client),
                gateway_latency_for(client),
            )
            raise
        except asyncio.TimeoutError:
            state = self._connection
            client = state.voice_client
            LOGGER.warning(
                "Voice websocket receive timed out after 30s: flow_state=%s guild_id=%s "
                "channel_id=%s endpoint=%s voice_latency=%s gateway_latency=%s",
                state.state.name,
                client.guild.id,
                client.channel.id,
                state.endpoint,
                voice_latency_for(client),
                gateway_latency_for(client),
            )
            raise


class DiagnosticVoiceConnectionState(VoiceConnectionState):
    async def _connect_websocket(self, resume: bool) -> DiscordVoiceWebSocket:
        previous_ws = getattr(self, "ws", None)
        seq_ack = getattr(previous_ws, "seq_ack", -1)
        LOGGER.info(
            "Opening voice websocket: resume=%s flow_state=%s guild_id=%s "
            "channel_id=%s endpoint=%s",
            resume,
            self.state.name,
            self.guild.id,
            self.voice_client.channel.id,
            self.endpoint,
        )
        ws = await DiagnosticVoiceWebSocket.from_connection_state(
            self,
            resume=resume,
            hook=self.hook,
            seq_ack=seq_ack,
        )
        self.state = ConnectionFlowState.websocket_connected
        return ws

    async def voice_state_update(self, data: dict[str, Any]) -> None:
        previous_state = self.state.name
        previous_session_id = self.session_id
        await super().voice_state_update(data)
        LOGGER.info(
            "Raw bot voice state update processed: channel_id=%s session_changed=%s "
            "flow_state=%s->%s disconnected_signal=%s expecting_disconnect=%s",
            data.get("channel_id"),
            previous_session_id is not None and data.get("session_id") != previous_session_id,
            previous_state,
            self.state.name,
            self._disconnected.is_set(),
            self._expecting_disconnect,
        )

    async def voice_server_update(self, data: dict[str, Any]) -> None:
        previous_state = self.state.name
        previous_endpoint = self.endpoint
        previous_token = self.token
        LOGGER.info(
            "Voice server update received: guild_id=%s endpoint=%s endpoint_changed=%s "
            "token_changed=%s flow_state=%s",
            data.get("guild_id"),
            data.get("endpoint"),
            previous_endpoint is not None and data.get("endpoint") != previous_endpoint,
            previous_token is not None and data.get("token") != previous_token,
            previous_state,
        )
        await super().voice_server_update(data)
        LOGGER.info(
            "Voice server update processed: flow_state=%s->%s endpoint=%s",
            previous_state,
            self.state.name,
            self.endpoint,
        )


class DiagnosticVoiceClient(discord.VoiceClient):
    last_voice_close_code: int | None
    last_voice_close_at: float | None

    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        self.last_voice_close_code = None
        self.last_voice_close_at = None
        super().__init__(client, channel)

    def create_connection_state(self) -> VoiceConnectionState:
        return DiagnosticVoiceConnectionState(self)


class ConfigurationError(ValueError):
    """Raised when required environment configuration is missing or invalid."""


class VoiceOperationError(RuntimeError):
    """Raised when a requested voice operation cannot be completed."""


class UnauthorizedUser(app_commands.CheckFailure):
    """Raised when a user is not included in the controller allowlist."""


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    authorized_user_ids: frozenset[int]
    state_path: Path = DEFAULT_STATE_PATH

    @classmethod
    def from_env(cls, state_path: Path = DEFAULT_STATE_PATH) -> "Config":
        load_dotenv(BASE_DIR / ".env")

        token = os.getenv("DISCORD_TOKEN", "").strip()
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

        return cls(
            token=token,
            guild_id=guild_id,
            authorized_user_ids=frozenset(authorized_user_ids),
            state_path=state_path,
        )


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
    def __init__(self, bot: commands.Bot, guild_id: int, store: StateStore):
        self.bot = bot
        self.guild_id = guild_id
        self.store = store
        self.state = store.load()
        self.last_error: str | None = None
        self.started_at = time.monotonic()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._watchdog_task: asyncio.Task[None] | None = None
        self._backoff = RetryBackoff()
        self._connection_attempts = 0

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
                await self._connect_locked(channel)
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
            guild = self.bot.get_guild(self.guild_id)
            voice_client = guild.voice_client if guild is not None else None
            if voice_client is not None:
                await voice_client.disconnect(force=True)
            self._mark_success()
        self.wake()

    async def rejoin(self) -> discord.VoiceChannel:
        async with self._lock:
            channel = self._resolve_target_locked()
            self.state.enabled = True
            self.store.save(self.state)

            guild = self.bot.get_guild(self.guild_id)
            voice_client = guild.voice_client if guild is not None else None
            try:
                if voice_client is not None:
                    await voice_client.disconnect(force=True)
                await channel.connect(
                    timeout=30.0,
                    reconnect=True,
                    self_deaf=True,
                    cls=DiagnosticVoiceClient,
                )
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
            await self._connect_locked(channel)
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
            guild = self.bot.get_guild(self.guild_id)
            voice_client = guild.voice_client if guild is not None else None
            if voice_client is not None:
                try:
                    await voice_client.disconnect(force=True)
                except discord.DiscordException:
                    LOGGER.exception("Failed to disconnect cleanly during shutdown")

    def status(self) -> dict[str, Any]:
        guild = self.bot.get_guild(self.guild_id)
        voice_client = guild.voice_client if guild is not None else None
        connected = bool(voice_client is not None and voice_client.is_connected())
        actual_channel = voice_client.channel if connected else None
        return {
            "enabled": self.state.enabled,
            "target_channel_id": self.state.target_channel_id,
            "connected": connected,
            "actual_channel": actual_channel,
            "uptime_seconds": int(time.monotonic() - self.started_at),
            "last_error": self.last_error,
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

    async def _connect_locked(self, channel: discord.VoiceChannel) -> None:
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            raise VoiceOperationError("The configured Discord server is currently unavailable.")

        voice_client = guild.voice_client
        if voice_client is not None and voice_client.is_connected():
            if voice_client.channel.id != channel.id:
                LOGGER.info("Moving to voice channel %s (%s)", channel.name, channel.id)
                await voice_client.move_to(channel)
            return

        if voice_client is not None:
            connection_state = getattr(getattr(voice_client, "_connection", None), "state", None)
            last_close_code = getattr(voice_client, "last_voice_close_code", None)
            last_close_at = getattr(voice_client, "last_voice_close_at", None)
            close_age = (
                f"{time.monotonic() - last_close_at:.1f}s"
                if last_close_at is not None
                else "unavailable"
            )
            LOGGER.warning(
                "Replacing existing disconnected voice client: client_id=%s flow_state=%s "
                "current_channel_id=%s target_channel_id=%s last_close_code=%s "
                "last_close_age=%s (an in-progress library reconnect may be racing recovery)",
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
            "Starting managed voice connection: attempt=%s channel=%s channel_id=%s",
            attempt,
            channel.name,
            channel.id,
        )
        connected_client = await channel.connect(
            timeout=30.0,
            reconnect=True,
            self_deaf=True,
            cls=DiagnosticVoiceClient,
        )
        LOGGER.info(
            "Managed voice connection succeeded: attempt=%s elapsed=%.2fs client_id=%s "
            "endpoint=%s voice_latency=%s gateway_latency=%s",
            attempt,
            time.monotonic() - started_at,
            id(connected_client),
            getattr(connected_client, "endpoint", None),
            voice_latency_for(connected_client),
            gateway_latency_for(connected_client),
        )

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


def format_uptime(total_seconds: int) -> str:
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    if days or hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


async def require_authorized_user(
    interaction: discord.Interaction,
    authorized_user_ids: frozenset[int],
) -> bool:
    if interaction.guild is None or interaction.user.id not in authorized_user_ids:
        raise UnauthorizedUser("This user is not authorized to control the bot.")
    return True


def build_voice_commands(
    manager: VoiceManager,
    authorized_user_ids: frozenset[int],
) -> app_commands.Group:
    group = app_commands.Group(name="voice", description="Manage the persistent voice connection")

    async def authorize(interaction: discord.Interaction) -> bool:
        return await require_authorized_user(interaction, authorized_user_ids)

    @group.command(name="join", description="Join your current voice channel and keep reconnecting")
    @app_commands.check(authorize)
    async def join(interaction: discord.Interaction) -> None:
        member = interaction.user
        channel = member.voice.channel if isinstance(member, discord.Member) and member.voice else None
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message(
                "Join a regular voice channel first, then run this command again.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await manager.connect_to(channel)
        except Exception as exc:
            LOGGER.exception("Could not join requested voice channel")
            await interaction.followup.send(
                f"Saved {channel.mention} as the target, but could not connect yet: {exc}",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Connected to {channel.mention}. Automatic recovery is enabled.",
            ephemeral=True,
        )

    @group.command(name="move", description="Move to a selected voice channel and save it")
    @app_commands.describe(channel="The voice channel the bot should stay in")
    @app_commands.check(authorize)
    async def move(interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await manager.connect_to(channel)
        except Exception as exc:
            LOGGER.exception("Could not move to requested voice channel")
            await interaction.followup.send(
                f"Saved {channel.mention} as the target, but could not connect yet: {exc}",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Moved to {channel.mention}. Automatic recovery is enabled.",
            ephemeral=True,
        )

    @group.command(name="leave", description="Disconnect and disable automatic recovery")
    @app_commands.check(authorize)
    async def leave(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await manager.leave()
        except Exception as exc:
            LOGGER.exception("Could not leave voice cleanly")
            await interaction.followup.send(f"Could not disconnect cleanly: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            "Disconnected. Automatic recovery is disabled; the last target is still saved.",
            ephemeral=True,
        )

    @group.command(name="rejoin", description="Reconnect to the saved target and enable recovery")
    @app_commands.check(authorize)
    async def rejoin(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await manager.rejoin()
        except Exception as exc:
            LOGGER.exception("Could not rejoin saved voice channel")
            await interaction.followup.send(f"Could not rejoin: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Reconnected to {channel.mention}. Automatic recovery is enabled.",
            ephemeral=True,
        )

    @group.command(name="status", description="Show the current voice connection state")
    @app_commands.check(authorize)
    async def status(interaction: discord.Interaction) -> None:
        snapshot = manager.status()
        target_id = snapshot["target_channel_id"]
        target = f"<#{target_id}> (`{target_id}`)" if target_id else "Not configured"
        actual_channel = snapshot["actual_channel"]
        actual = (
            f"{actual_channel.mention} (`{actual_channel.id}`)"
            if actual_channel is not None
            else "Disconnected"
        )
        error = snapshot["last_error"] or "None"
        message = (
            f"**Recovery:** {'Enabled' if snapshot['enabled'] else 'Disabled'}\n"
            f"**Target:** {target}\n"
            f"**Connection:** {actual}\n"
            f"**Uptime:** {format_uptime(snapshot['uptime_seconds'])}\n"
            f"**Last error:** {error}"
        )
        await interaction.response.send_message(message, ephemeral=True)

    return group


class VoiceBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

        self.config = config
        self.voice_manager = VoiceManager(self, config.guild_id, StateStore(config.state_path))
        self.tree.on_error = self.on_app_command_error
        self._gateway_connect_count = 0
        self._gateway_disconnected_at: float | None = None

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.guild_id)
        self.tree.add_command(
            build_voice_commands(self.voice_manager, self.config.authorized_user_ids),
            guild=guild,
        )
        synced = await self.tree.sync(guild=guild)
        LOGGER.info("Synced %d command group(s) to guild %s", len(synced), self.config.guild_id)
        self.voice_manager.start()

    async def on_ready(self) -> None:
        LOGGER.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            LOGGER.warning("Configured guild %s is unavailable", self.config.guild_id)
        else:
            LOGGER.info("Configured guild: %s (%s)", guild.name, guild.id)
        self.voice_manager.wake()

    async def on_connect(self) -> None:
        self._gateway_connect_count += 1
        disconnected_for = (
            f"{time.monotonic() - self._gateway_disconnected_at:.2f}s"
            if self._gateway_disconnected_at is not None
            else "initial connection"
        )
        LOGGER.info(
            "Discord gateway connected: connection_number=%s disconnected_for=%s latency=%s",
            self._gateway_connect_count,
            disconnected_for,
            format_latency(self.latency),
        )

    async def on_disconnect(self) -> None:
        self._gateway_disconnected_at = time.monotonic()
        snapshot = self.voice_manager.status()
        actual_channel = snapshot["actual_channel"]
        LOGGER.warning(
            "Discord gateway disconnected: latency=%s voice_connected=%s "
            "voice_channel_id=%s recovery_enabled=%s target_channel_id=%s",
            format_latency(self.latency),
            snapshot["connected"],
            getattr(actual_channel, "id", None),
            snapshot["enabled"],
            snapshot["target_channel_id"],
        )

    async def on_resumed(self) -> None:
        disconnected_for = (
            f"{time.monotonic() - self._gateway_disconnected_at:.2f}s"
            if self._gateway_disconnected_at is not None
            else "unknown"
        )
        LOGGER.info(
            "Discord gateway session resumed: disconnected_for=%s latency=%s",
            disconnected_for,
            format_latency(self.latency),
        )
        self._gateway_disconnected_at = None

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.user is not None and member.id == self.user.id:
            guild_voice_client = member.guild.voice_client
            connection_state = getattr(
                getattr(guild_voice_client, "_connection", None), "state", None
            )
            LOGGER.info(
                "Bot voice state changed: before_channel_id=%s after_channel_id=%s "
                "session_changed=%s self_mute=%s self_deaf=%s server_mute=%s "
                "server_deaf=%s suppressed=%s voice_client_id=%s connected=%s flow_state=%s",
                getattr(before.channel, "id", None),
                getattr(after.channel, "id", None),
                before.session_id != after.session_id,
                after.self_mute,
                after.self_deaf,
                after.mute,
                after.deaf,
                after.suppress,
                id(guild_voice_client) if guild_voice_client is not None else None,
                guild_voice_client.is_connected() if guild_voice_client is not None else False,
                getattr(connection_state, "name", "none"),
            )
            if before.channel != after.channel:
                self.voice_manager.wake()

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, UnauthorizedUser):
            message = "You are not authorized to control this bot."
        else:
            LOGGER.error(
                "Unhandled application command error",
                exc_info=(type(error), error, error.__traceback__),
            )
            message = "The command failed unexpectedly. Check the bot logs for details."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def close(self) -> None:
        await self.voice_manager.shutdown()
        await super().close()


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


if __name__ == "__main__":
    main()
