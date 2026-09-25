import threading
import time

import pytest
from discord_speak_bot.api.server import PREFIX, create_app
from discord_speak_bot.runtime import Runtime
from discord_speak_bot.settings.store import ConfigStore
from discord_speak_bot.tts.engine import FakeEngine
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    runtime = Runtime(ConfigStore(tmp_path), FakeEngine(), fake=True)
    with TestClient(
        create_app(runtime, "x" * 32),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + "x" * 32},
    ) as client:
        yield client


def test_auth_host_origin_body_limits(client):
    assert client.get(PREFIX + "/health").status_code == 200
    assert client.get(PREFIX + "/health", headers={"Authorization": "wrong"}).status_code == 401
    assert client.get(PREFIX + "/health", headers={"Host": "evil.example"}).status_code == 403
    assert client.get(PREFIX + "/health", headers={"Origin": "http://localhost"}).status_code == 403
    assert client.post(PREFIX + "/tts/test", content=b"x" * 65537).status_code == 413


def test_revision_and_override_reset(client):
    path = PREFIX + "/users/123"
    assert client.patch(path, json={"volume_percent": 0}).status_code == 428
    assert (
        client.patch(path, json={"volume_percent": 0}, headers={"If-Match": '"1"'}).status_code
        == 200
    )
    assert (
        client.patch(path, json={"volume_percent": 5}, headers={"If-Match": '"1"'}).status_code
        == 412
    )
    guild_path = PREFIX + "/guilds/9/users/123"
    result = client.patch(guild_path, json={"volume_percent": 80}, headers={"If-Match": '"2"'})
    assert result.json()["effective"]["volume_percent"] == 80
    result = client.delete(guild_path + "/overrides/volume_percent", headers={"If-Match": '"3"'})
    assert result.json()["effective"]["volume_percent"] == 0
    assert result.json()["sources"]["volume_percent"] == "global"


@pytest.mark.parametrize(
    "value", [{"speed_percent": 0}, {"voice_id": None}, {"unknown": 10}, {"speed_percent": "100"}]
)
def test_invalid_patch(client, value):
    assert (
        client.patch(PREFIX + "/users/123", json=value, headers={"If-Match": '"1"'}).status_code
        == 422
    )


def test_fake_preview_end_to_end_idempotency(client):
    for _ in range(50):
        if client.get(PREFIX + "/status").json()["engine_ready"]:
            break
        time.sleep(0.01)
    headers = {"Idempotency-Key": "test-preview"}
    first = client.post(PREFIX + "/tts/test", json={"text": "テスト"}, headers=headers)
    second = client.post(PREFIX + "/tts/test", json={"text": "テスト"}, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    operation = first.json()["operation_id"]
    assert (
        client.post(PREFIX + "/tts/test", json={"text": "別の文章"}, headers=headers).status_code
        == 409
    )
    for _ in range(100):
        result = client.get(PREFIX + f"/operations/{operation}").json()
        if result["state"] == "completed":
            break
        time.sleep(0.01)
    audio = client.get(PREFIX + f"/operations/{operation}/audio")
    assert audio.status_code == 200
    assert audio.content[:4] == b"RIFF"


def test_preview_waits_in_order_and_completed_history_does_not_count(tmp_path):
    gate = threading.Event()

    class SlowEngine(FakeEngine):
        def synthesize(self, job):
            if not gate.wait(10):
                raise TimeoutError("preview test gate timed out")
            return super().synthesize(job)

    runtime = Runtime(ConfigStore(tmp_path), SlowEngine(), fake=True)
    with TestClient(
        create_app(runtime, "x" * 32),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + "x" * 32},
    ) as client:
        try:
            for _ in range(50):
                if client.get(PREFIX + "/status").json()["engine_ready"]:
                    break
                time.sleep(0.01)
            ids = []
            for index in range(8):
                response = client.post(
                    PREFIX + "/tts/test",
                    json={"text": f"試聴{index}"},
                    headers={"Idempotency-Key": f"queue-{index}"},
                )
                assert response.status_code == 202
                ids.append(response.json()["operation_id"])
            response = client.post(
                PREFIX + "/tts/test",
                json={"text": "上限"},
                headers={"Idempotency-Key": "queue-full"},
            )
            assert response.status_code == 429
        finally:
            gate.set()
        for _ in range(200):
            states = [client.get(PREFIX + f"/operations/{item}").json()["state"] for item in ids]
            if states == ["completed"] * 8:
                break
            time.sleep(0.01)
        assert states == ["completed"] * 8
        response = client.post(
            PREFIX + "/tts/test",
            json={"text": "新しい試聴"},
            headers={"Idempotency-Key": "after-history"},
        )
        assert response.status_code == 202


def test_guild_limit_and_validation(client):
    for number in range(1, 4):
        response = client.patch(
            PREFIX + f"/guilds/{number}",
            json={"enabled": False},
            headers={"If-Match": f'"{number}"'},
        )
        assert response.status_code == 200
    assert (
        client.patch(PREFIX + "/guilds/4", json={}, headers={"If-Match": '"4"'}).status_code == 422
    )
    assert client.get(PREFIX + "/users/not-an-id").status_code == 422
