from __future__ import annotations

import discord
from discord import app_commands

from .constants import LOGGER
from .diagnostics import format_uptime
from .manager import VoiceManager


class UnauthorizedUser(app_commands.CheckFailure):
    """Raised when a user is not included in the controller allowlist."""


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
        seat = snapshot["active_seat"] or "None"
        seat_age = snapshot["seat_age_seconds"]
        next_rotation = snapshot["next_rotation_seconds"]
        message = (
            f"**Recovery:** {'Enabled' if snapshot['enabled'] else 'Disabled'}\n"
            f"**Target:** {target}\n"
            f"**Connection:** {actual}\n"
            f"**Voice seat:** {seat} (pool of {snapshot['pool_size']})\n"
            f"**Seat age:** {format_uptime(seat_age) if seat_age is not None else 'n/a'}\n"
            f"**Next rotation:** {format_uptime(next_rotation) if next_rotation is not None else 'n/a'}\n"
            f"**Uptime:** {format_uptime(snapshot['uptime_seconds'])}\n"
            f"**Last error:** {error}"
        )
        await interaction.response.send_message(message, ephemeral=True)

    return group
