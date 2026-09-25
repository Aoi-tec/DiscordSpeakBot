import io
import time
import wave

from ..settings.models import Voice, Voices, dump
from ..settings.store import atomic_write


def register_voice(
    store, voice_id: str, name: str, text: str, wav_bytes: bytes, allowed_user_ids=()
):
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
        name=name,
        reference_audio=f"voices/{voice_id}/reference.wav",
        reference_text=text,
        allowed_user_ids=list(allowed_user_ids),
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


def in_use_by_jobs(runtime, voice_id):
    for guild in runtime.scheduler.guilds.values():
        jobs = [*guild.text, *(audio.job for audio in guild.generated)]
        jobs += [guild.generating] if guild.generating else []
        jobs += [guild.playing.job] if guild.playing else []
        if any(job.settings.voice_id == voice_id for job in jobs):
            return True
    if any(job.settings.voice_id == voice_id for job in runtime.preview_pending):
        return True
    return any(op["state"] == "running" for op in runtime.operations.values())


def forget_user_references(store, voice_id, keep_user_ids=None):
    """Remove voice_id from users' settings (all users, or those not in keep_user_ids)."""

    def mutate(target):
        for user_id, user in target["users"].items():
            if keep_user_ids is not None and user_id in keep_user_ids:
                continue
            for layer in [user.get("global_settings", {}), *user.get("guilds", {}).values()]:
                if layer.get("voice_id") == voice_id:
                    layer.pop("voice_id")

    store.update("users", store.get("users").revision, mutate)


def remove_voice(store, runtime, voice_id, revision):
    with store.lock:
        voice = store.get("voices").voices.get(voice_id)
        if voice is None:
            raise KeyError(voice_id)
        if store.get("system").defaults.voice_id == voice_id:
            raise ValueError("既定ボイスは削除できません。先に既定ボイスを変更してください")
        if in_use_by_jobs(runtime, voice_id):
            raise ValueError("読み上げ待ちで使用中です。少し待つか clear してください")
        result = store.update("voices", revision, lambda target: target["voices"].pop(voice_id))
        # Users who picked this voice fall back to their other layers / the default.
        forget_user_references(store, voice_id)
        # Never delete: move the reference audio aside so the ID can be registered again.
        directory = store.root / "voices" / voice_id
        if directory.is_dir():
            trash = store.root / "voices" / ".deleted"
            trash.mkdir(exist_ok=True)
            directory.rename(trash / f"{voice_id}-{time.strftime('%Y%m%d-%H%M%S')}")
        return result


def update_voice(store, voice_id, revision, patch):
    """Change name / reference_text / allowed_user_ids of a registered voice."""
    if set(patch) - {"name", "reference_text", "allowed_user_ids"}:
        raise ValueError("変更できない項目です")
    with store.lock:
        current = store.get("voices").voices.get(voice_id)
        if current is None:
            raise KeyError(voice_id)
        allowed = patch.get("allowed_user_ids", current.allowed_user_ids)
        if allowed and store.get("system").defaults.voice_id == voice_id:
            raise ValueError("既定ボイスは専用ボイスにできません。先に既定ボイスを変更してください")

        def mutate(target):
            voice = target["voices"][voice_id]
            text_changed = (
                "reference_text" in patch and patch["reference_text"] != voice["reference_text"]
            )
            voice.update(patch)
            if text_changed:
                # Invalidates the cached clone profile and jobs made with the old prompt.
                voice["generation"] += 1

        result = store.update("voices", revision, mutate)
        if allowed:
            forget_user_references(store, voice_id, keep_user_ids=set(allowed))
        return result
