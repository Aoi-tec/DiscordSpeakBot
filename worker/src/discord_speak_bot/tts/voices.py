import io
import wave

from ..settings.models import Voice, Voices, dump
from ..settings.store import atomic_write


def register_voice(store, voice_id: str, name: str, text: str, wav_bytes: bytes):
    if len(wav_bytes) > 20 * 1024**2:
        raise ValueError("参照WAVの上限は20MiBです")
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
            if (
                not 1 <= duration <= 30
                or wav.getnchannels() not in (1, 2)
                or wav.getsampwidth() != 2
            ):
                raise ValueError("1〜30秒の16bit PCM mono/stereo WAVを指定してください")
            expected = wav.getnframes() * wav.getnchannels() * wav.getsampwidth()
            if len(wav.readframes(wav.getnframes())) != expected:
                raise ValueError("参照WAVが途中で切れています")
    except (wave.Error, EOFError, ZeroDivisionError):
        raise ValueError("有効な16bit PCM WAVを指定してください") from None
    voice = Voice(
        name=name, reference_audio=f"voices/{voice_id}/reference.wav", reference_text=text
    )
    Voices.model_validate({"voices": {voice_id: dump(voice)}})
    with store.lock:
        document = store.get("voices")
        if voice_id in document.voices:
            raise ValueError("Voice IDは登録済みです")
        directory = store.root / "voices" / voice_id
        directory.mkdir(parents=True, exist_ok=False)
        try:
            atomic_write(directory / "reference.wav", wav_bytes)
            return store.update(
                "voices",
                document.revision,
                lambda target: target["voices"].update({voice_id: dump(voice)}),
            )
        except Exception:
            (directory / "reference.wav").unlink(missing_ok=True)
            directory.rmdir()
            raise


def referenced(store, runtime, voice_id):
    if store.get("system").defaults.voice_id == voice_id:
        return True
    for user in store.get("users").users.values():
        if any(
            layer.voice_id == voice_id for layer in [user.global_settings, *user.guilds.values()]
        ):
            return True
    for guild in runtime.scheduler.guilds.values():
        jobs = [*guild.text, *(audio.job for audio in guild.generated)]
        jobs += [guild.generating] if guild.generating else []
        jobs += [guild.playing.job] if guild.playing else []
        if any(job.settings.voice_id == voice_id for job in jobs):
            return True
    if runtime.preview_pending and runtime.preview_pending.settings.voice_id == voice_id:
        return True
    return any(op["state"] == "running" for op in runtime.operations.values())


def remove_voice(store, runtime, voice_id, revision):
    with store.lock:
        if referenced(store, runtime, voice_id):
            raise ValueError("参照中のVoiceです")
        voice = store.get("voices").voices.get(voice_id)
        if voice is None:
            raise KeyError(voice_id)
        result = store.update("voices", revision, lambda target: target["voices"].pop(voice_id))
        # Configuration is the source of truth. Leave files on disk for deliberate recovery;
        # never delete arbitrary paths found in imported JSON.
        return result
