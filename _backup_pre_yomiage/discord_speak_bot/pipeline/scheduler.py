import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from ..settings.models import QueueConfig, SpeechSettings

PCM_BYTES_PER_SECOND = 48000 * 2 * 2


@dataclass
class SpeechJob:
    guild_id: str
    user_id: str
    text: str
    settings: SpeechSettings
    voice_generation: int
    received_at: float
    expires_at: float
    message_ids: list[str] = field(default_factory=list)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    sequence: int = 0
    epoch: int = 0
    state: str = "queued"


@dataclass
class AudioJob:
    job: SpeechJob
    pcm: bytes


@dataclass
class GuildQueue:
    connected: bool = False
    epoch: int = 0
    sequence: int = 0
    text: deque = field(default_factory=deque)
    generated: deque = field(default_factory=deque)
    generating: SpeechJob | None = None
    playing: AudioJob | None = None


class Scheduler:
    """Event-loop confined. Reservation remains held until cancelled inference actually ends."""

    def __init__(self, config: QueueConfig):
        self.config = config
        self.guilds: dict[str, GuildQueue] = {}
        self.order = deque()
        self.reserved_bytes = 0
        self.audio_bytes = 0
        self.active: SpeechJob | None = None
        self.rejected = 0

    @property
    def reservation(self):
        return self.config.max_audio_seconds * PCM_BYTES_PER_SECOND

    def guild(self, guild_id):
        if guild_id not in self.guilds:
            self.guilds[guild_id] = GuildQueue()
            self.order.append(guild_id)
        return self.guilds[guild_id]

    def enqueue(self, job: SpeechJob, limit=None) -> bool:
        guild = self.guild(job.guild_id)
        if not guild.connected or len(guild.text) >= min(
            limit or self.config.max_text_queue, self.config.max_text_queue
        ):
            self.rejected += 1
            return False
        guild.sequence += 1
        job.sequence, job.epoch = guild.sequence, guild.epoch
        guild.text.append(job)
        return True

    def next(self, now=None) -> SpeechJob | None:
        if self.active:
            return None
        now = time.monotonic() if now is None else now
        if self.audio_bytes + self.reservation > self.config.audio_budget_mib * 1024**2:
            return None
        for _ in range(len(self.order)):
            guild_id = self.order[0]
            self.order.rotate(-1)
            guild = self.guilds[guild_id]
            while guild.text and guild.text[0].expires_at <= now:
                guild.text.popleft().state = "expired"
            if (
                not guild.connected
                or not guild.text
                or len(guild.generated) >= self.config.max_generated_queue
            ):
                continue
            job = guild.text.popleft()
            job.state = "generating"
            guild.generating = self.active = job
            self.reserved_bytes = self.reservation
            return job
        return None

    def finish(self, job: SpeechJob, pcm: bytes | None, now=None):
        if self.active is not job:
            return
        guild = self.guilds[job.guild_id]
        now = time.monotonic() if now is None else now
        self.active = guild.generating = None
        self.reserved_bytes = 0
        if job.state == "cancelled" or job.epoch != guild.epoch or not guild.connected:
            job.state = "cancelled"
        elif job.expires_at <= now:
            job.state = "expired"
        elif not pcm or len(pcm) > self.reservation:
            job.state = "failed"
        else:
            job.state = "generated"
            guild.generated.append(AudioJob(job, pcm))
            self.audio_bytes += len(pcm)

    def take_audio(self, guild_id, now=None):
        guild = self.guild(guild_id)
        if guild.playing or not guild.connected:
            return None
        now = time.monotonic() if now is None else now
        while guild.generated:
            audio = guild.generated.popleft()
            if audio.job.expires_at <= now:
                self.audio_bytes -= len(audio.pcm)
                audio.job.state = "expired"
                continue
            guild.playing = audio
            audio.job.state = "playing"
            return audio
        return None

    def playback_done(self, guild_id, request_id, failed=False):
        guild = self.guild(guild_id)
        audio = guild.playing
        if audio and audio.job.request_id == request_id:
            self.audio_bytes -= len(audio.pcm)
            if audio.job.state != "cancelled":
                audio.job.state = "failed" if failed else "completed"
            guild.playing = None

    def clear(self, guild_id):
        guild = self.guild(guild_id)
        guild.epoch += 1
        for job in guild.text:
            job.state = "cancelled"
        guild.text.clear()
        if guild.generating:
            guild.generating.state = "cancelled"
        for audio in guild.generated:
            audio.job.state = "cancelled"
            self.audio_bytes -= len(audio.pcm)
        guild.generated.clear()

    def skip(self, guild_id):
        guild = self.guild(guild_id)
        if guild.playing:
            guild.playing.job.state = "cancelled"
            return "playing"
        if guild.generated:
            audio = guild.generated.popleft()
            audio.job.state = "cancelled"
            self.audio_bytes -= len(audio.pcm)
        elif guild.generating:
            guild.generating.state = "cancelled"
        elif guild.text:
            guild.text.popleft().state = "cancelled"
        return "pending"

    def disconnect(self, guild_id):
        guild = self.guild(guild_id)
        guild.connected = False
        self.clear(guild_id)
        if guild.playing:
            guild.playing.job.state = "cancelled"
            self.playback_done(guild_id, guild.playing.job.request_id)

    def status(self):
        return {
            key: {
                "connected": g.connected,
                "text": len(g.text),
                "generated": len(g.generated),
                "generating": bool(g.generating),
                "playing": bool(g.playing),
            }
            for key, g in self.guilds.items()
        }
