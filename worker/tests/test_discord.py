from discord_speak_bot.discord_adapter.bot import PCMSource, SpeakBot
from discord_speak_bot.runtime import Runtime
from discord_speak_bot.settings.store import ConfigStore
from discord_speak_bot.tts.engine import FakeEngine


def test_audio_frame_size_and_eof():
    source = PCMSource(b"1234")
    assert len(source.read()) == 3840
    assert source.read() == b""
    source.cleanup()


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
