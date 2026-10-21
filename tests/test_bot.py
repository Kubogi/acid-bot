import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from acid_bot.commands import UnauthorizedUser, require_authorized_user
from acid_bot.config import Config, ConfigurationError
from acid_bot.diagnostics import (
    DiagnosticVoiceClient,
    describe_voice_close,
    format_latency,
    format_uptime,
    voice_latency_for,
)
from acid_bot.manager import RetryBackoff, VoiceManager, VoiceOperationError
from acid_bot.state import PersistentVoiceState, StateStore


class ConfigTests(unittest.TestCase):
    def test_loads_valid_environment(self):
        environment = {
            "DISCORD_TOKEN": "secret",
            "GUILD_ID": "123",
            "AUTHORIZED_USER_IDS": "42, 99,42",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_env(Path("state.json"))
        self.assertEqual(config.token, "secret")
        self.assertEqual(config.voice_tokens, ())
        self.assertEqual(config.rotation_seconds, 10800.0)
        self.assertEqual(config.guild_id, 123)
        self.assertEqual(config.authorized_user_ids, frozenset({42, 99}))

    def test_parses_voice_token_pool_and_rotation(self):
        environment = {
            "DISCORD_TOKEN": "secret",
            "GUILD_ID": "123",
            "AUTHORIZED_USER_IDS": "42",
            "VOICE_BOT_TOKENS": "tok-a, tok-b ,secret,tok-a,",
            "VOICE_ROTATION_SECONDS": "900",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_env(Path("state.json"))
        self.assertEqual(config.voice_tokens, ("tok-a", "tok-b"))
        self.assertEqual(config.rotation_seconds, 900.0)

    def test_rejects_invalid_rotation_seconds(self):
        base = {
            "DISCORD_TOKEN": "secret",
            "GUILD_ID": "123",
            "AUTHORIZED_USER_IDS": "42",
        }
        for value in ("abc", "-5", "0", "inf"):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ, {**base, "VOICE_ROTATION_SECONDS": value}, clear=True
                ):
                    with self.assertRaises(ConfigurationError):
                        Config.from_env(Path("state.json"))

    def test_rejects_missing_or_invalid_values(self):
        cases = [
            {},
            {"DISCORD_TOKEN": "secret"},
            {
                "DISCORD_TOKEN": "secret",
                "GUILD_ID": "not-a-number",
                "AUTHORIZED_USER_IDS": "42",
            },
            {"DISCORD_TOKEN": "secret", "GUILD_ID": "0", "AUTHORIZED_USER_IDS": "42"},
            {"DISCORD_TOKEN": "secret", "GUILD_ID": "123"},
            {
                "DISCORD_TOKEN": "secret",
                "GUILD_ID": "123",
                "AUTHORIZED_USER_IDS": "not-a-number",
            },
            {"DISCORD_TOKEN": "secret", "GUILD_ID": "123", "AUTHORIZED_USER_IDS": "0"},
            {"DISCORD_TOKEN": "secret", "GUILD_ID": "123", "AUTHORIZED_USER_IDS": "42,"},
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
        self.bot.is_closed.return_value = False
        self.bot.is_ready.return_value = True
        self.bot._acid_login_failed = False
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

    def make_pool_member(self, voice_client=None, channel=None):
        guild = MagicMock(spec=discord.Guild)
        guild.voice_client = voice_client
        guild.get_channel = MagicMock(return_value=channel)
        client = MagicMock()
        client.is_closed.return_value = False
        client.is_ready.return_value = True
        client._acid_login_failed = False
        client.get_guild.return_value = guild
        client.user = None
        return client, guild

    async def test_connect_persists_target_and_enables_recovery(self):
        channel = self.make_channel()
        await self.manager.connect_to(channel)

        channel.connect.assert_awaited_once_with(
            timeout=30.0,
            reconnect=True,
            self_deaf=True,
            cls=DiagnosticVoiceClient,
        )
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
        self.manager._active_index = 0

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

    def make_connected_voice_client(self, channel, events=None, tag="disconnect"):
        voice_client = MagicMock(spec=discord.VoiceClient)
        voice_client.is_connected.return_value = True
        voice_client.channel = channel
        voice_client.move_to = AsyncMock()
        if events is None:
            voice_client.disconnect = AsyncMock()
        else:
            voice_client.disconnect = AsyncMock(
                side_effect=lambda **kwargs: events.append(tag)
            )
        return voice_client

    def make_unconnected_channel(self, channel, events=None, tag="connect"):
        seat_channel = MagicMock(spec=discord.VoiceChannel)
        seat_channel.id = channel.id
        seat_channel.name = channel.name
        new_voice_client = MagicMock(spec=discord.VoiceClient)
        new_voice_client.channel = seat_channel
        if events is None:
            seat_channel.connect = AsyncMock(return_value=new_voice_client)
        else:
            async def connect(**kwargs):
                events.append(tag)
                return new_voice_client

            seat_channel.connect = AsyncMock(side_effect=connect)
        return seat_channel, new_voice_client

    async def test_handover_connects_new_seat_before_releasing_old(self):
        channel = self.make_channel()
        events = []
        old_voice_client = self.make_connected_voice_client(channel, events, "old_disconnect")
        old_client, _ = self.make_pool_member(voice_client=old_voice_client)
        new_channel, _ = self.make_unconnected_channel(channel, events, "new_connect")
        new_client, _ = self.make_pool_member(channel=new_channel)

        self.manager.set_pool([old_client, new_client])
        self.manager._active_index = 0
        self.manager._seat_since = time.monotonic()
        self.manager.state = PersistentVoiceState(channel.id, True)

        await self.manager.rejoin()

        self.assertEqual(events, ["new_connect", "old_disconnect"])
        self.assertEqual(self.manager._active_index, 1)

    async def test_scheduled_rotation_swaps_seat(self):
        channel = self.make_channel()
        old_voice_client = self.make_connected_voice_client(channel)
        old_client, _ = self.make_pool_member(voice_client=old_voice_client)
        new_channel, _ = self.make_unconnected_channel(channel)
        new_client, _ = self.make_pool_member(channel=new_channel)

        self.manager.set_pool([old_client, new_client])
        self.manager._active_index = 0
        self.manager._seat_since = time.monotonic() - 9999
        self.manager.rotation_seconds = 60
        self.manager.state = PersistentVoiceState(channel.id, True)

        await self.manager.ensure_connected()

        new_channel.connect.assert_awaited_once()
        old_voice_client.disconnect.assert_awaited_once_with(force=True)
        self.assertEqual(self.manager._active_index, 1)

    async def test_dead_seat_rotates_to_next_member(self):
        channel = self.make_channel()
        self.voice_client.is_connected.return_value = False
        self.voice_client.channel = channel
        self.guild.voice_client = self.voice_client
        new_channel, _ = self.make_unconnected_channel(channel)
        new_client, _ = self.make_pool_member(channel=new_channel)

        self.manager.set_pool([self.bot, new_client])
        self.manager._active_index = 0
        self.manager._seat_since = time.monotonic()
        self.manager.state = PersistentVoiceState(channel.id, True)

        await self.manager.ensure_connected()

        new_channel.connect.assert_awaited_once()
        self.assertEqual(self.manager._active_index, 1)

    async def test_connected_seat_in_single_member_pool_never_rotates(self):
        channel = self.make_channel()
        self.voice_client.is_connected.return_value = True
        self.voice_client.channel = channel
        self.guild.voice_client = self.voice_client
        self.manager._active_index = 0
        self.manager._seat_since = time.monotonic() - 999_999
        self.manager.rotation_seconds = 60
        self.manager.state = PersistentVoiceState(channel.id, True)

        await self.manager.ensure_connected()

        channel.connect.assert_not_awaited()
        self.voice_client.disconnect.assert_not_awaited()
        self.assertEqual(self.manager._active_index, 0)

    async def test_rotation_failure_keeps_existing_seat(self):
        channel = self.make_channel()
        self.voice_client.is_connected.return_value = True
        self.voice_client.channel = channel
        self.guild.voice_client = self.voice_client

        failing_client, failing_guild = self.make_pool_member()
        failing_guild.get_channel.return_value = None

        self.manager.set_pool([self.bot, failing_client])
        self.manager._active_index = 0
        self.manager._seat_since = time.monotonic() - 9999
        self.manager.rotation_seconds = 60
        self.manager.state = PersistentVoiceState(channel.id, True)

        await self.manager.ensure_connected()

        self.assertEqual(self.manager._active_index, 0)
        self.voice_client.disconnect.assert_not_awaited()

    def test_set_pool_requires_members(self):
        with self.assertRaises(ValueError):
            self.manager.set_pool([])

    async def test_status_reports_seat_and_rotation(self):
        channel = self.make_channel()
        self.voice_client.is_connected.return_value = True
        self.voice_client.channel = channel
        self.guild.voice_client = self.voice_client
        await self.manager.connect_to(channel)

        snapshot = self.manager.status()

        self.assertEqual(snapshot["pool_size"], 1)
        self.assertTrue(snapshot["active_seat"].startswith("main[0]"))
        self.assertIsNotNone(snapshot["seat_age_seconds"])
        self.assertIsNone(snapshot["next_rotation_seconds"])


class UtilityTests(unittest.IsolatedAsyncioTestCase):
    def test_formats_uptime(self):
        self.assertEqual(format_uptime(65), "1m 5s")
        self.assertEqual(format_uptime(90_061), "1d 1h 1m 1s")

    def test_describes_voice_close_codes(self):
        self.assertIn("gateway", describe_voice_close(4014))
        self.assertIn("server crashed", describe_voice_close(4015))
        self.assertIn("unknown", describe_voice_close(4999))

    def test_formats_latency_safely(self):
        self.assertEqual(format_latency(0.1234), "123ms")
        self.assertEqual(format_latency(float("inf")), "unavailable")
        self.assertEqual(format_latency(None), "unavailable")

        client = MagicMock()
        type(client).latency = property(lambda _: (_ for _ in ()).throw(RuntimeError("closed")))
        self.assertEqual(voice_latency_for(client), "unavailable")

    async def test_user_must_be_in_controller_allowlist(self):
        allowed = SimpleNamespace(
            guild=object(),
            user=SimpleNamespace(id=42),
        )
        denied = SimpleNamespace(
            guild=object(),
            user=SimpleNamespace(id=7),
        )

        self.assertTrue(await require_authorized_user(allowed, frozenset({42, 99})))
        with self.assertRaises(UnauthorizedUser):
            await require_authorized_user(denied, frozenset({42, 99}))


if __name__ == "__main__":
    unittest.main()
