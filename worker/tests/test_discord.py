import pytest
from discord_speak_bot.discord_adapter.bot import (
    GuildLimitError,
    PCMSource,
    SpeakBot,
    configure_first_join,
    ensure_guild,
)
from discord_speak_bot.discord_adapter.views import bar, excerpt
from discord_speak_bot.runtime import Runtime
from discord_speak_bot.settings.models import Guilds
from discord_speak_bot.settings.store import ConfigStore
from discord_speak_bot.tts.engine import FakeEngine


def test_audio_frame_size_and_eof():
    source = PCMSource(b"1234")
    assert len(source.read()) == 3840
    assert source.read() == b""
    source.cleanup()


def test_first_join_configures_guild_without_overwriting_existing_choice(tmp_path):
    store = ConfigStore(tmp_path)
    configure_first_join(store, "123", "456", "789")
    configured = store.get("guilds").guilds["123"]
    assert configured.enabled
    assert configured.text_channel_ids == ["456"]
    assert configured.voice_channel_id == "789"
    configure_first_join(store, "123", "111", "222")
    assert store.get("guilds").guilds["123"] == configured


def test_ensure_guild_adds_channels_and_limits_guilds(tmp_path):
    store = ConfigStore(tmp_path)
    assert ensure_guild(store, "1", text_channel_id="10")
    assert ensure_guild(store, "1", text_channel_id="11")
    assert not ensure_guild(store, "1", text_channel_id="10")
    assert store.get("guilds").guilds["1"].text_channel_ids == ["10", "11"]
    ensure_guild(store, "2")
    ensure_guild(store, "3")
    with pytest.raises(GuildLimitError):
        ensure_guild(store, "4")


def test_legacy_single_text_channel_is_migrated():
    document = Guilds.model_validate_json(
        '{"guilds": {"1": {"enabled": true, "text_channel_id": "5", "voice_channel_id": "6"}}}'
    )
    assert document.guilds["1"].text_channel_ids == ["5"]


def test_ui_helpers():
    assert bar(5, 10) == "▰▰▰▰▰▱▱▱▱▱"
    assert bar(20, 10) == "▰" * 10
    assert excerpt("a" * 100, 10) == "a" * 9 + "…"
    assert "@​everyone" in excerpt("@everyone")


async def test_bot_command_registration_without_network(tmp_path):
    runtime = Runtime(ConfigStore(tmp_path), FakeEngine(), fake=True)
    bot = SpeakBot(runtime)
    assert {c.name for c in bot.tree.get_commands()} == {"yomiage"}
    group = bot.tree.get_command("yomiage")
    assert {c.name for c in group.commands} == {
        "join",
        "leave",
        "add",
        "remove",
        "list",
        "voice",
        "speed",
        "skip",
        "clear",
        "reset",
        "all",
        "status",
        "queue",
        "permission",
    }
    assert [c.name for c in group.get_command("add").commands] == ["channel"]
    assert [c.name for c in group.get_command("remove").commands] == ["channel"]
    assert [c.name for c in group.get_command("all").commands] == ["reset"]
    assert [c.name for c in group.get_command("permission").commands] == ["clear"]
    # Payload must be accepted by Discord's schema (names, nesting, option types).
    payload = group.to_dict(bot.tree)
    assert payload["name"] == "yomiage"
    await bot.close()
    runtime.executor.shutdown()


async def test_leaves_voice_channel_when_no_humans_remain(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    runtime = Runtime(ConfigStore(tmp_path), FakeEngine(), fake=True)
    bot = SpeakBot(runtime)
    bot.leave = AsyncMock()
    human = SimpleNamespace(bot=False)
    other_bot = SimpleNamespace(bot=True)
    channel = SimpleNamespace(id=2, members=[other_bot, human])
    guild = SimpleNamespace(id=1, voice_client=SimpleNamespace(channel=channel))

    await bot.leave_if_alone(guild)
    bot.leave.assert_not_awaited()

    channel.members = [other_bot]
    await bot.leave_if_alone(guild)
    bot.leave.assert_awaited_once_with("1")

    await bot.leave_if_alone(SimpleNamespace(id=3, voice_client=None))
    assert bot.leave.await_count == 1
    await bot.close()
    runtime.executor.shutdown()


def test_clear_permission_by_role_toggle(tmp_path):
    from types import SimpleNamespace

    runtime = Runtime(ConfigStore(tmp_path), FakeEngine(), fake=True)
    bot = SpeakBot(runtime)
    ensure_guild(runtime.store, "1")
    member = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_guild=False), roles=[SimpleNamespace(id=77)]
    )
    interaction = SimpleNamespace(guild=True, guild_id=1, user=member)
    bot.can_manage = lambda _: False
    assert not bot.can_clear(interaction)

    def enable(flag, roles):
        runtime.store.update(
            "guilds",
            runtime.store.get("guilds").revision,
            lambda t: t["guilds"]["1"].update(clear_by_role=flag, clear_role_ids=roles),
        )

    enable(True, ["77"])
    assert bot.can_clear(interaction)
    enable(False, ["77"])
    assert not bot.can_clear(interaction)
    bot.can_manage = lambda _: True
    assert bot.can_clear(interaction)
    runtime.executor.shutdown()


def test_join_leave_announcements_use_the_members_voice(tmp_path):
    from types import SimpleNamespace

    runtime = Runtime(ConfigStore(tmp_path), FakeEngine(), fake=True)
    runtime.engine_ready = True
    bot = SpeakBot(runtime)
    ensure_guild(runtime.store, "1", text_channel_id="10")
    runtime.store.update(
        "users",
        runtime.store.get("users").revision,
        lambda t: t["users"].update(
            {"7": {"global_settings": {"speed_percent": 130}, "guilds": {}}}
        ),
    )
    runtime.scheduler.guild("1").connected = True
    here, elsewhere = SimpleNamespace(id=20), SimpleNamespace(id=21)
    guild = SimpleNamespace(id=1, voice_client=SimpleNamespace(channel=here))
    member = SimpleNamespace(id=7, bot=False, display_name="たろう", guild=guild)
    state = lambda channel: SimpleNamespace(channel=channel)  # noqa: E731

    bot.announce_voice_state(member, state(None), state(here))
    bot.announce_voice_state(member, state(here), state(elsewhere))
    bot.announce_voice_state(member, state(None), state(elsewhere))  # other VC: silent
    bot.announce_voice_state(
        SimpleNamespace(**{**vars(member), "bot": True}), state(None), state(here)
    )
    jobs = runtime.scheduler.snapshot("1")["text"]
    assert [job.text for job in jobs] == ["たろうさんが入室しました", "たろうさんが退室しました"]
    assert all(job.user_id == "7" and job.settings.speed_percent == 130 for job in jobs)

    runtime.store.update(
        "guilds",
        runtime.store.get("guilds").revision,
        lambda t: t["guilds"]["1"].update(announce_voice_state=False),
    )
    bot.announce_voice_state(member, state(None), state(here))
    assert len(runtime.scheduler.snapshot("1")["text"]) == 2
    runtime.executor.shutdown()
