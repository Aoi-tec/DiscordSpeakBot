import json

import pytest
from discord_speak_bot.settings.models import Overrides, System, Users, dump, resolve
from discord_speak_bot.settings.store import ConfigStore, DataLock, RevisionConflict, StoreError
from pydantic import ValidationError


def test_field_level_inheritance_and_zero():
    users = Users.model_validate(
        {
            "users": {
                "123": {
                    "global_settings": {"voice_id": "a", "speed_percent": 120, "volume_percent": 0},
                    "guilds": {"9": {"voice_id": "b"}},
                }
            }
        }
    )
    effective, sources = resolve(System(), users, "123", "9")
    assert dump(effective) == {"voice_id": "b", "speed_percent": 120, "volume_percent": 0}
    assert sources == {"voice_id": "guild", "speed_percent": "global", "volume_percent": "global"}


@pytest.mark.parametrize(
    "value",
    [
        {"speed": 30},
        {"speed_percent": 30},
        {"speed_percent": "100"},
        {"voice_id": None},
        {"volume_percent": True},
    ],
)
def test_invalid_overrides(value):
    with pytest.raises(ValidationError):
        Overrides.model_validate(value)


def test_store_persistence_and_backup(tmp_path):
    store = ConfigStore(tmp_path)
    store.update("system", 1, lambda value: value["defaults"].update(speed_percent=150))
    assert ConfigStore(tmp_path).get("system").defaults.speed_percent == 150
    (tmp_path / "system.json").write_text("broken")
    recovered = ConfigStore(tmp_path)
    assert recovered.get("system").defaults.speed_percent == 100
    assert recovered.recovered == ["system"]


def test_missing_primary_uses_backup(tmp_path):
    store = ConfigStore(tmp_path)
    store.update("system", 1, lambda value: value["defaults"].update(speed_percent=150))
    (tmp_path / "system.json").unlink()
    assert ConfigStore(tmp_path).recovered == ["system"]


def test_both_corrupt_not_reset(tmp_path):
    ConfigStore(tmp_path)
    (tmp_path / "system.json").write_text("broken")
    (tmp_path / "system.json.bak").write_text("broken")
    with pytest.raises(StoreError):
        ConfigStore(tmp_path)
    assert (tmp_path / "system.json").read_text() == "broken"


def test_failed_save_does_not_publish_ram(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    import discord_speak_bot.settings.store as module

    original = module.atomic_write

    def fail_primary(path, content):
        if path.name == "system.json":
            raise OSError("disk full")
        original(path, content)

    monkeypatch.setattr(module, "atomic_write", fail_primary)
    with pytest.raises(OSError):
        store.update("system", 1, lambda value: value["defaults"].update(speed_percent=150))
    assert store.get("system").revision == 1
    assert json.loads((tmp_path / "system.json").read_text())["revision"] == 1


def test_revision_and_snapshot_isolation(tmp_path):
    store = ConfigStore(tmp_path)
    snapshot = store.get("system")
    snapshot.defaults.speed_percent = 150
    assert store.get("system").defaults.speed_percent == 100
    with pytest.raises(RevisionConflict):
        store.update("system", 2, lambda target: None)


def test_process_lock(tmp_path):
    first = DataLock(tmp_path)
    try:
        with pytest.raises(StoreError):
            DataLock(tmp_path)
    finally:
        first.close()
    DataLock(tmp_path).close()
