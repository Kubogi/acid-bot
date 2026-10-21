from __future__ import annotations

import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands

from .commands import UnauthorizedUser, build_voice_commands
from .config import Config
from .constants import LOGGER
from .diagnostics import format_latency, log_voice_state_change
from .manager import VoiceManager
from .state import StateStore


class VoiceBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

        self.config = config
        self.voice_manager = VoiceManager(
            self,
            config.guild_id,
            StateStore(config.state_path),
            rotation_seconds=config.rotation_seconds,
        )
        self.secondary_clients = [
            self._build_secondary_client(index) for index in range(len(config.voice_tokens))
        ]
        self._secondary_tasks: list[asyncio.Task[None]] = []
        self.tree.on_error = self.on_app_command_error
        self._gateway_connect_count = 0
        self._gateway_disconnected_at: float | None = None

    def _build_secondary_client(self, index: int) -> discord.Client:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        client = discord.Client(intents=intents)
        pool_index = index + 1  # index 0 in the pool is the main bot

        @client.event
        async def on_ready() -> None:
            LOGGER.info(
                "Secondary bot logged in: pool_index=%s user=%s user_id=%s",
                pool_index,
                client.user,
                client.user.id if client.user else "unknown",
            )
            self.voice_manager.wake()

        @client.event
        async def on_disconnect() -> None:
            LOGGER.warning(
                "Secondary bot gateway disconnected: pool_index=%s", pool_index
            )

        @client.event
        async def on_resumed() -> None:
            LOGGER.info(
                "Secondary bot gateway session resumed: pool_index=%s", pool_index
            )

        @client.event
        async def on_voice_state_update(
            member: discord.Member,
            before: discord.VoiceState,
            after: discord.VoiceState,
        ) -> None:
            if client.user is None or member.id != client.user.id:
                return
            log_voice_state_change(member, before, after)
            if before.channel != after.channel:
                self.voice_manager.wake()

        return client

    async def _run_secondary_client(self, index: int, client: discord.Client) -> None:
        try:
            await client.start(self.config.voice_tokens[index])
        except asyncio.CancelledError:
            raise
        except Exception:
            client._acid_login_failed = True
            LOGGER.exception("Secondary bot %s stopped unexpectedly", index + 1)

    async def setup_hook(self) -> None:
        self.voice_manager.set_pool([self, *self.secondary_clients])
        for index, client in enumerate(self.secondary_clients):
            self._secondary_tasks.append(
                asyncio.create_task(
                    self._run_secondary_client(index, client),
                    name=f"secondary-bot-{index + 1}",
                )
            )
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
            log_voice_state_change(member, before, after)
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
        for client in self.secondary_clients:
            if not client.is_closed():
                try:
                    await client.close()
                except Exception:
                    LOGGER.exception("Failed to close a secondary bot cleanly")
        if self._secondary_tasks:
            for task in self._secondary_tasks:
                task.cancel()
            await asyncio.gather(*self._secondary_tasks, return_exceptions=True)
        await super().close()
