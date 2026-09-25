from discord_speak_bot.discord_adapter.bot import PCMSource, SpeakBot, configure_first_join
from discord_speak_bot.runtime import Runtime
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
    assert configured.text_channel_id == "456"
    assert configured.voice_channel_id == "789"
    configure_first_join(store, "123", "111", "222")
    assert store.get("guilds").guilds["123"] == configured


async def test_bot_command_registration_without_network(tmp_path):
    runtime = Runtime(ConfigStore(tmp_path), FakeEngine(), fake=True)
    bot = SpeakBot(runtime)
    assert {c.name for c in bot.tree.get_commands()} == {"voice", "speed", "volume", "tts"}
    assert {c.name for c in bot.tree.get_command("tts").commands} == {
        "join",
        "leave",
        "skip",
        "clear",
        "reset",
        "status",
    }
    await bot.close()
    runtime.executor.shutdown()
