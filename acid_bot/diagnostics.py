from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import discord
from discord.errors import ConnectionClosed
from discord.gateway import DiscordVoiceWebSocket
from discord.voice_state import ConnectionFlowState, VoiceConnectionState

from .constants import LOGGER


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


def log_voice_state_change(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
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
