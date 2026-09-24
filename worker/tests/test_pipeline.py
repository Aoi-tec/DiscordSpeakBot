from discord_speak_bot.pipeline.messages import Aggregator, Message, clean_text, split_text
from discord_speak_bot.pipeline.scheduler import Scheduler, SpeechJob
from discord_speak_bot.settings.models import QueueConfig, SpeechSettings


def message(user, text, now):
    return Message("1", "2", user, text, now)


def job(guild="1", expires=100):
    return SpeechJob(guild, "123", "こんにちは", SpeechSettings(), 1, 0, expires)


def scheduler(**kwargs):
    result = Scheduler(QueueConfig(**kwargs))
    for guild in ("1", "2", "3"):
        result.guild(guild).connected = True
    return result


def test_aggregation_preserves_intervening_speaker():
    agg = Aggregator()
    assert agg.push(message("a", "一", 0), 200) == []
    assert [m.text for m in agg.push(message("b", "二", 0.05), 200)] == ["一"]
    assert [m.text for m in agg.push(message("a", "三", 0.1), 200)] == ["二"]
    assert [m.text for m in agg.flush_due(0.4)] == ["三"]


def test_aggregation_fixed_deadline():
    agg = Aggregator()
    agg.push(message("a", "一", 0), 200)
    agg.push(message("a", "二", 0.15), 200)
    assert [m.text for m in agg.flush_due(0.201)] == ["一、二"]


def test_filter_and_bounded_split():
    assert (
        clean_text("こんにちは https://example.com ```secret``` <:smile:123>") == "こんにちは smile"
    )
    parts, truncated = split_text("あ" * 601, 200, 3)
    assert list(map(len, parts)) == [200, 200, 200]
    assert truncated


def test_fair_order():
    queue = scheduler()
    for guild in ("1", "1", "1", "2", "3"):
        queue.enqueue(job(guild))
    order = []
    for _ in range(3):
        active = queue.next(0)
        order.append(active.guild_id)
        queue.finish(active, b"1234", 0)
    assert order == ["1", "2", "3"]


def test_clear_during_generation_discards_late_result():
    queue = scheduler()
    active = job()
    queue.enqueue(active)
    queue.next(0)
    queue.clear("1")
    assert queue.reserved_bytes > 0
    assert queue.next(0) is None
    queue.finish(active, b"1234", 0)
    assert not queue.guild("1").generated
    assert active.state == "cancelled"
    assert queue.reserved_bytes == 0
    assert queue.audio_bytes == 0


def test_clear_keeps_current_playback_skip_stops_it():
    queue = scheduler()
    first = job()
    queue.enqueue(first)
    queue.finish(queue.next(0), b"1234", 0)
    audio = queue.take_audio("1", 0)
    queue.clear("1")
    assert queue.guild("1").playing is audio
    assert queue.skip("1") == "playing"
    queue.playback_done("1", first.request_id)
    queue.playback_done("1", first.request_id)  # repeated callback must not decrement twice
    assert queue.audio_bytes == 0
    assert first.state == "cancelled"


def test_generated_capacity_and_reservation():
    queue = scheduler(max_generated_queue=1)
    queue.enqueue(job())
    queue.enqueue(job())
    queue.finish(queue.next(0), b"1234", 0)
    assert queue.next(0) is None
    queue.take_audio("1", 0)
    assert queue.next(0) is not None


def test_expiry_failure_and_queue_full():
    queue = scheduler(max_text_queue=1)
    expired = job(expires=1)
    assert queue.enqueue(expired)
    assert not queue.enqueue(job())
    assert queue.next(2) is None
    assert expired.state == "expired"
    queue.enqueue(job())
    active = queue.next(2)
    queue.finish(active, None, 2)
    assert queue.reserved_bytes == 0
    assert active.state == "failed"


def test_disconnect_cannot_replay_old_audio():
    queue = scheduler()
    queue.enqueue(job())
    active = queue.next(0)
    queue.disconnect("1")
    queue.guild("1").connected = True
    queue.finish(active, b"1234", 0)
    assert queue.take_audio("1", 0) is None


def test_skip_oldest_generated_before_inflight():
    queue = scheduler()
    first, second = job(), job()
    queue.enqueue(first)
    queue.enqueue(second)
    queue.finish(queue.next(0), b"1234", 0)
    queue.next(0)
    queue.skip("1")
    assert first.state == "cancelled"
    assert second.state == "generating"
