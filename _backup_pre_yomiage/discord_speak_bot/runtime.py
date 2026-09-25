import asyncio
import io
import logging
import time
import uuid
import wave
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor

from .pipeline.messages import Aggregator, Message, clean_text, split_text
from .pipeline.scheduler import Scheduler, SpeechJob
from .settings.models import resolve

log = logging.getLogger("worker")


class Runtime:
    def __init__(self, store, engine, *, fake=False):
        self.store, self.engine, self.fake = store, engine, fake
        self.instance_id = uuid.uuid4().hex
        self.config = store.get("system")
        self.scheduler = Scheduler(self.config.queue)
        self.aggregator = Aggregator(self.config.queue.max_text_chars)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
        self.state = "starting"
        self.last_error = None
        self.engine_ready = False
        self.restart_requested = False
        self.discord = None
        self.stopping = asyncio.Event()
        self.tasks = []
        self.seen = OrderedDict()
        self.operations = {}
        self.preview_pending = deque()
        self.generation_started = None
        self.metrics = {"completed": 0, "failed": 0, "truncated": 0, "ttfa_ms": None}

    async def start(self):
        self.tasks = [asyncio.create_task(self._load()), asyncio.create_task(self._pump())]

    async def _load(self):
        try:
            await asyncio.get_running_loop().run_in_executor(self.executor, self.engine.load)
            self.engine_ready = True
            self.state = "ready" if self.fake else "degraded"
        except Exception as exc:
            self.last_error = (
                str(exc) if isinstance(exc, ValueError) else f"ENGINE_LOAD_{type(exc).__name__}"
            )
            self.state = "degraded"
            log.error("engine_load_failed type=%s", type(exc).__name__)

    def status(self):
        connected = bool(self.discord and self.discord.is_ready())
        voice = self.store.get("system").defaults.voice_id
        ready = self.engine_ready and (
            self.fake or (connected and voice in self.store.get("voices").voices)
        )
        state = self.state
        if state not in ("starting", "stopping", "failed"):
            state = "ready" if ready else "degraded"
        return {
            "process_instance_id": self.instance_id,
            "state": state,
            "engine_ready": self.engine_ready,
            "restart_requested": self.restart_requested,
            "development_mode": self.fake,
            "discord_connected": connected,
            "discord_guilds": [
                {"id": str(guild.id), "name": guild.name} for guild in self.discord.guilds
            ]
            if connected
            else [],
            "last_error": self.last_error,
            "guilds": self.scheduler.status(),
            "metrics": self.metrics,
            "audio_bytes": self.scheduler.audio_bytes,
            "reserved_bytes": self.scheduler.reserved_bytes,
            "preview_waiting": len(self.preview_pending),
            "generation_age_seconds": time.monotonic() - self.generation_started
            if self.generation_started
            else None,
            "inference_timeout_seconds": self.config.tts.inference_timeout_seconds,
            "recovered_settings": self.store.recovered,
        }

    def ingest(self, guild_id, channel_id, user_id, message_id, text, *, is_bot=False):
        if self.stopping.is_set() or not self.engine_ready:
            return
        config = self.store.get("guilds").guilds.get(guild_id)
        if not config or not config.enabled or config.text_channel_id != channel_id:
            return
        if is_bot and not config.read_bot_messages:
            return
        if not self.scheduler.guild(guild_id).connected or message_id in self.seen:
            return
        self.seen[message_id] = None
        if len(self.seen) > 4096:
            self.seen.popitem(last=False)
        text = clean_text(text, skip_urls=config.skip_urls, skip_codeblocks=config.skip_codeblocks)
        if not text:
            return
        item = Message(guild_id, channel_id, user_id, text, time.monotonic(), [message_id])
        for ready in self.aggregator.push(item, config.merge_window_ms):
            self._enqueue(ready)

    def _enqueue(self, item):
        config = self.store.get("guilds").guilds.get(item.guild_id)
        if not config or not config.enabled:
            return
        system = self.store.get("system")
        settings, _ = resolve(system, self.store.get("users"), item.user_id, item.guild_id)
        voices = self.store.get("voices").voices
        if settings.voice_id not in voices:
            settings.voice_id = system.defaults.voice_id
        voice = voices.get(settings.voice_id)
        if not voice and not self.fake:
            self.last_error = "NO_VOICE"
            return
        segments, truncated = split_text(
            item.text, self.config.queue.max_text_chars, self.config.queue.max_segments
        )
        self.metrics["truncated"] += int(truncated)
        for text in segments:
            job = SpeechJob(
                item.guild_id,
                item.user_id,
                text,
                settings.model_copy(deep=True),
                voice.generation if voice else 0,
                item.received_at,
                item.received_at + self.config.queue.job_ttl_seconds,
                item.message_ids,
            )
            self.scheduler.enqueue(job, config.max_queue)

    def submit_preview(self, text, settings):
        self._prune_operations()
        if not self.engine_ready:
            raise RuntimeError("ENGINE_NOT_READY")
        in_flight = len(self.preview_pending) + sum(
            operation["state"] == "running" for operation in self.operations.values()
        )
        if in_flight >= 8:
            raise RuntimeError("PREVIEW_BUSY")
        voices = self.store.get("voices").voices
        voice = voices.get(settings.voice_id)
        if not voice and not self.fake:
            raise ValueError("VOICE_NOT_FOUND")
        now = time.monotonic()
        job = SpeechJob("0", "0", text, settings, voice.generation if voice else 0, now, now + 600)
        self.operations[job.request_id] = {
            "state": "pending",
            "created": now,
            "pcm": None,
            "error": None,
        }
        self.preview_pending.append(job)
        return job.request_id

    def _prune_operations(self):
        now = time.monotonic()
        for key, value in list(self.operations.items()):
            if now - value["created"] > 600 and value["state"] not in ("running", "pending"):
                del self.operations[key]
        # Completed WAVs must not fill all memory during repeated previews.
        retained = sum(len(value["pcm"]) for value in self.operations.values() if value["pcm"])
        for key, value in list(self.operations.items()):
            if retained <= 64 * 1024**2:
                break
            if value["state"] == "completed" and value["pcm"]:
                retained -= len(value["pcm"])
                del self.operations[key]

    def preview_wav(self, operation_id):
        pcm = self.operations[operation_id]["pcm"]
        if pcm is None:
            return None
        output = io.BytesIO()
        with wave.open(output, "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(48000)
            stream.writeframes(pcm)
        return output.getvalue()

    async def _generate(self, job, preview=False):
        self.generation_started = time.monotonic()
        pcm = None
        try:
            pcm = await asyncio.get_running_loop().run_in_executor(
                self.executor, self.engine.synthesize, job
            )
            self.metrics["ttfa_ms"] = round((time.monotonic() - self.generation_started) * 1000, 1)
            self.metrics["completed"] += 1
            log.info("generated request_id=%s ttfa_ms=%s", job.request_id, self.metrics["ttfa_ms"])
            timing = getattr(self.engine, "last_timing", None)
            if timing:
                self.metrics["last_synthesis"] = timing.copy()
                log.info(
                    "synth_timing request_id=%s profile_ms=%s inference_ms=%s postprocess_ms=%s audio_seconds=%s",
                    job.request_id,
                    timing["profile_ms"],
                    timing["inference_ms"],
                    timing["postprocess_ms"],
                    timing["audio_seconds"],
                )
        except Exception as exc:
            self.last_error = f"SYNTHESIS_{type(exc).__name__}"
            self.metrics["failed"] += 1
            log.error("generation_failed request_id=%s type=%s", job.request_id, type(exc).__name__)
            if "OutOfMemory" in type(exc).__name__ or isinstance(exc, RuntimeError):
                self.engine_ready = False
                self.restart_requested = True
                self.state = "degraded"
        finally:
            self.generation_started = None
            if preview:
                operation = self.operations[job.request_id]
                operation.update(
                    state="completed" if pcm else "failed",
                    pcm=pcm,
                    error=None if pcm else self.last_error,
                )
            else:
                self.scheduler.finish(job, pcm)

    async def _pump(self):
        generating = None
        try:
            while not self.stopping.is_set():
                for message in self.aggregator.flush_due(time.monotonic()):
                    self._enqueue(message)
                self._prune_operations()
                if generating and generating.done():
                    await generating
                    generating = None
                if not generating and self.engine_ready:
                    job = self.scheduler.next()
                    if job:
                        generating = asyncio.create_task(self._generate(job))
                    elif self.preview_pending:
                        job = self.preview_pending.popleft()
                        self.operations[job.request_id]["state"] = "running"
                        generating = asyncio.create_task(self._generate(job, preview=True))
                if self.discord:
                    self.discord.pump_playback()
                await asyncio.sleep(0.02)
        finally:
            if generating:
                generating.cancel()
                await asyncio.gather(generating, return_exceptions=True)

    def clear(self, guild_id):
        self.aggregator.clear(guild_id)
        self.scheduler.clear(guild_id)

    async def stop(self):
        self.state = "stopping"
        self.stopping.set()
        for guild_id in list(self.scheduler.guilds):
            self.aggregator.clear(guild_id)
            self.scheduler.disconnect(guild_id)
        if self.discord:
            await self.discord.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # CUDA may still be executing. The Host enforces the hard shutdown deadline.
        self.executor.shutdown(wait=False, cancel_futures=True)
