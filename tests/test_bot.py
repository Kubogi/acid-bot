import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord import app_commands

from bot import (
    Config,
    ConfigurationError,
    PersistentVoiceState,
    RetryBackoff,
    StateStore,
    VoiceManager,
    VoiceOperationError,
    format_uptime,
    require_manage_guild,
)


class ConfigTests(unittest.TestCase):
    def test_loads_valid_environment(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN": "secret", "GUILD_ID": "123"}, clear=True):
            config = Config.from_env(Path("state.json"))
        self.assertEqual(config.token, "secret")
        self.assertEqual(config.guild_id, 123)

    def test_rejects_missing_or_invalid_values(self):
        cases = [
            {},
            {"DISCORD_TOKEN": "secret"},
            {"DISCORD_TOKEN": "secret", "GUILD_ID": "not-a-number"},
            {"DISCORD_TOKEN": "secret", "GUILD_ID": "0"},
        ]
        for environment in cases:
            with self.subTest(environment=environment):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ConfigurationError):
                        Config.from_env(Path("state.json"))


class StateStoreTests(unittest.TestCase):
    def test_missing_state_uses_safe_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "missing.json").load()
        self.assertEqual(state, PersistentVoiceState())

    def test_state_round_trip_is_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            store = StateStore(path)
            expected = PersistentVoiceState(target_channel_id=456, enabled=True)
            store.save(expected)
            loaded = store.load()
            raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded, expected)
        self.assertEqual(raw, {"target_channel_id": 456, "enabled": True})

    def test_corrupt_state_uses_safe_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("not-json", encoding="utf-8")
            state = StateStore(path).load()
        self.assertEqual(state, PersistentVoiceState())


class RetryBackoffTests(unittest.TestCase):
    def test_backoff_caps_and_resets(self):
        backoff = RetryBackoff(5, 60)
        self.assertEqual([backoff.next_delay() for _ in range(6)], [5, 10, 20, 40, 60, 60])
        backoff.reset()
        self.assertEqual(backoff.next_delay(), 5)


class VoiceManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.store = StateStore(Path(self.temp_directory.name) / "state.json")

        self.voice_client = MagicMock(spec=discord.VoiceClient)
        self.voice_client.is_connected.return_value = False
        self.voice_client.disconnect = AsyncMock()
        self.voice_client.move_to = AsyncMock()

        self.guild = MagicMock(spec=discord.Guild)
        self.guild.voice_client = None
        self.guild.get_channel = MagicMock()

        self.bot = MagicMock()
        self.bot.get_guild.return_value = self.guild
        self.manager = VoiceManager(self.bot, 123, self.store)

    def make_channel(self, channel_id=456, name="General"):
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = channel_id
        channel.name = name
        channel.mention = f"<#{channel_id}>"
        channel.connect = AsyncMock(return_value=self.voice_client)
        self.guild.get_channel.return_value = channel
        return channel

    async def test_connect_persists_target_and_enables_recovery(self):
        channel = self.make_channel()
        await self.manager.connect_to(channel)

        channel.connect.assert_awaited_once_with(timeout=30.0, reconnect=True, self_deaf=True)
        self.assertEqual(self.store.load(), PersistentVoiceState(456, True))
        self.assertIsNone(self.manager.last_error)

    async def test_connected_client_moves_instead_of_reconnecting(self):
        current = self.make_channel(111, "Old")
        target = self.make_channel(222, "New")
        self.voice_client.is_connected.return_value = True
        self.voice_client.channel = current
        self.guild.voice_client = self.voice_client

        await self.manager.connect_to(target)

        self.voice_client.move_to.assert_awaited_once_with(target)
        target.connect.assert_not_awaited()
        self.assertEqual(self.store.load(), PersistentVoiceState(222, True))

    async def test_leave_disables_recovery_and_retains_target(self):
        self.store.save(PersistentVoiceState(456, True))
        self.manager.state = self.store.load()
        self.guild.voice_client = self.voice_client

        await self.manager.leave()

        self.voice_client.disconnect.assert_awaited_once_with(force=True)
        self.assertEqual(self.store.load(), PersistentVoiceState(456, False))

    async def test_rejoin_cycles_connection_and_enables_recovery(self):
        channel = self.make_channel()
        self.manager.state = PersistentVoiceState(456, False)
        self.guild.voice_client = self.voice_client

        result = await self.manager.rejoin()

        self.assertIs(result, channel)
        self.voice_client.disconnect.assert_awaited_once_with(force=True)
        channel.connect.assert_awaited_once()
        self.assertEqual(self.store.load(), PersistentVoiceState(456, True))

    async def test_rejoin_without_target_is_rejected(self):
        with self.assertRaises(VoiceOperationError):
            await self.manager.rejoin()

    async def test_voice_operations_are_serialized(self):
        channel = self.make_channel()
        active = 0
        maximum_active = 0

        async def slow_connect(**kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return self.voice_client

        channel.connect.side_effect = slow_connect
        await asyncio.gather(
            self.manager.connect_to(channel),
            self.manager.connect_to(channel),
        )
        self.assertEqual(maximum_active, 1)


class UtilityTests(unittest.IsolatedAsyncioTestCase):
    def test_formats_uptime(self):
        self.assertEqual(format_uptime(65), "1m 5s")
        self.assertEqual(format_uptime(90_061), "1d 1h 1m 1s")

    async def test_manage_server_permission_is_required(self):
        allowed = SimpleNamespace(
            guild=object(),
            user=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
        )
        denied = SimpleNamespace(
            guild=object(),
            user=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=False)),
        )

        self.assertTrue(await require_manage_guild(allowed))
        with self.assertRaises(app_commands.MissingPermissions):
            await require_manage_guild(denied)


if __name__ == "__main__":
    unittest.main()
