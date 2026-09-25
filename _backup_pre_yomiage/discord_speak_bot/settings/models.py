from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Snowflake = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,20}$")]
VoiceId = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class SpeechSettings(Model):
    voice_id: VoiceId | None = None
    speed_percent: int = Field(default=100, ge=50, le=200)
    volume_percent: int = Field(default=80, ge=0, le=100)


class Overrides(Model):
    voice_id: VoiceId | None = None
    speed_percent: int | None = Field(default=None, ge=50, le=200)
    volume_percent: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="before")
    @classmethod
    def no_null_values(cls, value):
        if isinstance(value, dict) and any(v is None for v in value.values()):
            raise ValueError("overrideはnullではなく削除してください")
        return value


class UserSettings(Model):
    global_settings: Overrides = Field(default_factory=Overrides)
    guilds: dict[Snowflake, Overrides] = Field(default_factory=dict)


class Document(Model):
    schema_version: Literal[1] = 1
    revision: int = Field(default=1, ge=1)


class Users(Document):
    users: dict[Snowflake, UserSettings] = Field(default_factory=dict)


class QueueConfig(Model):
    max_text_queue: int = Field(default=10, ge=1, le=100)
    max_generated_queue: int = Field(default=3, ge=1, le=10)
    merge_window_ms: int = Field(default=200, ge=0, le=2000)
    max_text_chars: int = Field(default=200, ge=10, le=1000)
    max_segments: int = Field(default=3, ge=1, le=10)
    job_ttl_seconds: int = Field(default=30, ge=1, le=300)
    max_audio_seconds: int = Field(default=30, ge=1, le=60)
    audio_budget_mib: int = Field(default=64, ge=16, le=512)


class GuildSettings(Model):
    enabled: bool = False
    text_channel_id: Snowflake | None = None
    voice_channel_id: Snowflake | None = None
    auto_rejoin: bool = False
    max_queue: int = Field(default=10, ge=1, le=100)
    merge_window_ms: int = Field(default=200, ge=0, le=2000)
    skip_urls: bool = True
    skip_codeblocks: bool = True
    read_bot_messages: bool = False
    admin_role_ids: list[Snowflake] = Field(default_factory=list, max_length=20)


class Guilds(Document):
    guilds: dict[Snowflake, GuildSettings] = Field(default_factory=dict, max_length=3)


class Voice(Model):
    name: str = Field(min_length=1, max_length=80)
    reference_audio: str = Field(min_length=1, max_length=240)
    reference_text: str = Field(min_length=1, max_length=2000)
    generation: int = Field(default=1, ge=1)


class Voices(Document):
    voices: dict[VoiceId, Voice] = Field(default_factory=dict, max_length=100)


class TTSConfig(Model):
    model_path: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    gpu_uuid: str = ""
    dtype: Literal["float16", "bfloat16"] = "bfloat16"
    max_new_tokens: int = Field(default=1024, ge=32, le=2048)
    inference_timeout_seconds: int = Field(default=120, ge=10, le=600)
    ffmpeg_path: str = "ffmpeg"


class Performance(Model):
    cpu_mode: Literal["automatic", "low_impact", "custom"] = "automatic"
    cpu_set_ids: list[int] = Field(default_factory=list, max_length=256)
    process_priority: Literal["normal", "below_normal"] = "below_normal"


class Startup(Model):
    start_with_windows: bool = False
    start_minimized: bool = True


class APIConfig(Model):
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)


class System(Document):
    startup: Startup = Field(default_factory=Startup)
    performance: Performance = Field(default_factory=Performance)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    defaults: SpeechSettings = Field(default_factory=SpeechSettings)


DOCUMENTS = {"system": System, "users": Users, "guilds": Guilds, "voices": Voices}


def dump(model: BaseModel) -> dict:
    return model.model_dump(mode="json", exclude_none=True)


def resolve(
    system: System, users: Users, user_id: str, guild_id: str
) -> tuple[SpeechSettings, dict]:
    values = dump(system.defaults)
    sources = dict.fromkeys(SpeechSettings.model_fields, "system")
    user = users.users.get(user_id)
    if user:
        layers = [("global", user.global_settings), ("guild", user.guilds.get(guild_id))]
        for source, layer in layers:
            if layer:
                for key, value in dump(layer).items():
                    values[key], sources[key] = value, source
    return SpeechSettings.model_validate(values), sources
