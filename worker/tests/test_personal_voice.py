import base64
import io
import uuid
import wave

from discord_speak_bot.api.server import PREFIX, create_app
from discord_speak_bot.discord_adapter.bot import ensure_guild
from discord_speak_bot.runtime import Runtime
from discord_speak_bot.settings.store import ConfigStore
from discord_speak_bot.tts.engine import FakeEngine
from fastapi.testclient import TestClient

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def wav_bytes(seconds=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\0\0" * 24000 * seconds)
    return output.getvalue()


def add(client, voice_id, allowed=None):
    body = {
        "voice_id": voice_id,
        "name": voice_id.upper(),
        "reference_text": "テスト",
        "wav_base64": base64.b64encode(wav_bytes()).decode(),
    }
    if allowed is not None:
        body["allowed_user_ids"] = allowed
    return client.post(
        PREFIX + "/voices", json=body, headers={**AUTH, "Idempotency-Key": uuid.uuid4().hex}
    )


def test_personal_voice_lifecycle(tmp_path):
    store = ConfigStore(tmp_path)
    runtime = Runtime(store, FakeEngine(), fake=True)
    with TestClient(create_app(runtime, TOKEN), base_url="http://127.0.0.1") as client:
        assert add(client, "pub").status_code == 201
        assert add(client, "mine", ["111"]).status_code == 201
        assert add(client, "pub").status_code == 409
        voices = store.get("voices").voices
        assert voices["mine"].allowed_user_ids == ["111"] and voices["pub"].usable_by("5")

        rev = store.get("system").revision
        restricted_default = client.patch(
            PREFIX + "/settings/system",
            json={"defaults": {"voice_id": "mine"}},
            headers={**AUTH, "If-Match": f'"{rev}"'},
        )
        assert restricted_default.status_code == 422
        client.patch(
            PREFIX + "/settings/system",
            json={"defaults": {"voice_id": "pub"}},
            headers={**AUTH, "If-Match": f'"{rev}"'},
        )

        # Another user cannot pick the personal voice through the API.
        users_rev = store.get("users").revision
        denied = client.patch(
            PREFIX + "/users/222",
            json={"voice_id": "mine"},
            headers={**AUTH, "If-Match": f'"{users_rev}"'},
        )
        assert denied.status_code == 403
        ok = client.patch(
            PREFIX + "/users/111",
            json={"voice_id": "mine"},
            headers={**AUTH, "If-Match": f'"{users_rev}"'},
        )
        assert ok.status_code == 200
        assert runtime.speech_settings("111", "1").voice_id == "mine"

        # Restricting to someone else removes 111's choice; they fall back to the default.
        rev = store.get("voices").revision
        changed = client.patch(
            PREFIX + "/voices/mine",
            json={"allowed_user_ids": ["333"], "reference_text": "新しい文"},
            headers={**AUTH, "If-Match": f'"{rev}"'},
        )
        assert changed.status_code == 200
        assert changed.json()["voice"]["generation"] == 2
        assert store.get("users").users["111"].global_settings.voice_id is None
        assert runtime.speech_settings("111", "1").voice_id == "pub"

        # Deleting frees the ID for re-registration; files are moved, not removed.
        rev = store.get("voices").revision
        assert (
            client.delete(
                PREFIX + "/voices/mine", headers={**AUTH, "If-Match": f'"{rev}"'}
            ).status_code
            == 200
        )
        assert list((tmp_path / "voices" / ".deleted").iterdir())
        assert add(client, "mine", ["111"]).status_code == 201
        rev = store.get("voices").revision
        assert (
            client.delete(
                PREFIX + "/voices/pub", headers={**AUTH, "If-Match": f'"{rev}"'}
            ).status_code
            == 409
        )


def test_enqueue_never_uses_someone_elses_personal_voice(tmp_path):
    store = ConfigStore(tmp_path)
    store.update(
        "voices",
        store.get("voices").revision,
        lambda t: t["voices"].update(
            {
                "pub": {"name": "P", "reference_audio": "voices/p.wav", "reference_text": "t"},
                "mine": {
                    "name": "M",
                    "reference_audio": "voices/m.wav",
                    "reference_text": "t",
                    "allowed_user_ids": ["111"],
                },
            }
        ),
    )
    store.update(
        "system", store.get("system").revision, lambda t: t["defaults"].update(voice_id="pub")
    )
    # Written directly (e.g. hand-edited users.json) to bypass the API check.
    store.update(
        "users",
        store.get("users").revision,
        lambda t: t["users"].update(
            {u: {"global_settings": {"voice_id": "mine"}, "guilds": {}} for u in ("111", "222")}
        ),
    )
    runtime = Runtime(store, FakeEngine(), fake=True)
    runtime.engine_ready = True
    ensure_guild(store, "1", text_channel_id="10")
    runtime.scheduler.guild("1").connected = True
    store.update(
        "guilds", store.get("guilds").revision, lambda t: t["guilds"]["1"].update(merge_window_ms=0)
    )
    runtime.ingest("1", "10", "111", "m1", "本人")
    runtime.ingest("1", "10", "222", "m2", "他人")
    jobs = runtime.scheduler.snapshot("1")["text"]
    assert [(job.user_id, job.settings.voice_id) for job in jobs] == [
        ("111", "mine"),
        ("222", "pub"),
    ]
    runtime.executor.shutdown()
