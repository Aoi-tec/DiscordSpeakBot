import asyncio

from discord_speak_bot.discord_adapter.bot import ensure_guild
from discord_speak_bot.runtime import Runtime
from discord_speak_bot.settings.store import ConfigStore
from discord_speak_bot.tts.engine import FakeEngine


def make_runtime(tmp_path):
    runtime = Runtime(ConfigStore(tmp_path), FakeEngine(), fake=True)
    runtime.engine_ready = True
    ensure_guild(runtime.store, "1", text_channel_id="10")
    ensure_guild(runtime.store, "1", text_channel_id="11")
    runtime.scheduler.guild("1").connected = True
    return runtime


def test_ingest_reads_every_registered_channel_only(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.ingest("1", "10", "u1", "m1", "いち")
    runtime.ingest("1", "11", "u2", "m2", "に")
    runtime.ingest("1", "99", "u3", "m3", "対象外")
    for message in runtime.aggregator.flush_due(float("inf")):
        runtime._enqueue(message)
    assert [job.text for job in runtime.scheduler.snapshot("1")["text"]] == ["いち", "に"]
    assert runtime.scheduler.pending_count("1") == 2
    runtime.executor.shutdown()


async def test_pump_wakes_on_new_work_without_polling(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.store.update(
        "guilds",
        runtime.store.get("guilds").revision,
        lambda target: target["guilds"]["1"].update(merge_window_ms=0),
    )
    pump = asyncio.create_task(runtime._pump())
    await asyncio.sleep(0.05)
    runtime.ingest("1", "10", "u1", "m1", "こんにちは")
    for _ in range(50):
        await asyncio.sleep(0.01)
        if runtime.scheduler.snapshot("1")["generated"]:
            break
    assert runtime.scheduler.snapshot("1")["generated"][0].text == "こんにちは"
    runtime.stopping.set()
    runtime.notify()
    await asyncio.wait_for(pump, 1)
    runtime.executor.shutdown()


def test_reset_user_is_personal(tmp_path):
    runtime = make_runtime(tmp_path)
    store = runtime.store
    store.update(
        "users",
        store.get("users").revision,
        lambda target: target["users"].update(
            {
                "5": {
                    "global_settings": {"speed_percent": 120},
                    "guilds": {"1": {"speed_percent": 90}, "2": {"speed_percent": 80}},
                },
                "6": {"global_settings": {"speed_percent": 150}, "guilds": {}},
            }
        ),
    )
    assert runtime.reset_user("5", "1")
    assert not runtime.reset_user("5", "1")
    user = store.get("users").users["5"]
    assert set(user.guilds) == {"2"} and user.global_settings.speed_percent == 120
    assert runtime.reset_user("5")
    assert set(store.get("users").users) == {"6"}
    assert store.get("guilds").guilds["1"].text_channel_ids == ["10", "11"]
    runtime.executor.shutdown()
