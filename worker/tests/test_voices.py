import base64
import io
import wave

import pytest
from discord_speak_bot.api.server import PREFIX, create_app
from discord_speak_bot.runtime import Runtime
from discord_speak_bot.settings.models import SpeechSettings
from discord_speak_bot.settings.store import ConfigStore
from discord_speak_bot.tts.engine import FakeEngine
from discord_speak_bot.tts.qwen import managed_path
from discord_speak_bot.tts.voices import register_voice
from fastapi.testclient import TestClient


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(bytes(48000))
    return output.getvalue()


def test_voice_api_upload_and_reference_guard(tmp_path):
    store = ConfigStore(tmp_path)
    runtime = Runtime(store, FakeEngine(), fake=True)
    with TestClient(
        create_app(runtime, "k" * 32),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + "k" * 32},
    ) as client:
        response = client.post(
            PREFIX + "/voices",
            headers={"Idempotency-Key": "voice"},
            json={
                "voice_id": "type-a",
                "name": "Type A",
                "reference_text": "こんにちは",
                "wav_base64": base64.b64encode(wav_bytes()).decode(),
            },
        )
        assert response.status_code == 201
        assert (tmp_path / "voices/type-a/reference.wav").is_file()
        client.patch(
            PREFIX + "/settings/system",
            json={"defaults": {"voice_id": "type-a"}},
            headers={"If-Match": '"1"'},
        )
        response = client.delete(PREFIX + "/voices/type-a", headers={"If-Match": '"2"'})
        assert response.status_code == 409
        assert "type-a" in store.get("voices").voices


@pytest.mark.parametrize("voice_id", ["../escape", "a/b", "..", "", "C:\\foo"])
def test_voice_path_rejected(tmp_path, voice_id):
    with pytest.raises(ValueError):
        register_voice(ConfigStore(tmp_path), voice_id, "name", "reference", wav_bytes())
    assert not (tmp_path / "voices").exists()


def test_managed_path_rejects_escape(tmp_path):
    outside = tmp_path / "outside.wav"
    outside.write_bytes(wav_bytes())
    with pytest.raises(ValueError):
        managed_path(tmp_path, str(outside))


def test_preview_capacity_is_bounded(tmp_path):
    runtime = Runtime(ConfigStore(tmp_path), FakeEngine(), fake=True)
    runtime.engine_ready = True
    runtime.submit_preview("a", SpeechSettings())
    with pytest.raises(RuntimeError, match="PREVIEW_BUSY"):
        runtime.submit_preview("b", SpeechSettings())
    runtime.executor.shutdown()
