import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path

from .models import DOCUMENTS, dump


class StoreError(Exception):
    pass


class RevisionConflict(StoreError):
    pass


def atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.025 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


class DataLock:
    """OS-owned lock: automatically released even after a process crash."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.stream = (root / ".worker.lock").open("a+b")
        try:
            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise StoreError("このdata-dirは別Workerが使用しています") from None

    def close(self):
        self.stream.close()


class ConfigStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.documents = {}
        self.recovered = []
        self.reload()

    def reload(self):
        with self.lock:
            loaded = {}
            for name, cls in DOCUMENTS.items():
                path = self.root / f"{name}.json"
                backup = path.with_suffix(".json.bak")
                if not path.exists() and not backup.exists():
                    model = cls()
                    atomic_write(path, self._encode(dump(model)))
                else:
                    try:
                        model = cls.model_validate_json(path.read_bytes())
                    except (OSError, ValueError):
                        try:
                            model = cls.model_validate_json(backup.read_bytes())
                        except (OSError, ValueError):
                            raise StoreError(f"{name}.jsonとbakを復旧できません") from None
                        atomic_write(path, self._encode(dump(model)))
                        self.recovered.append(name)
                loaded[name] = model
            self.documents = loaded

    @staticmethod
    def _encode(value):
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def get(self, name):
        with self.lock:
            return self.documents[name].model_copy(deep=True)

    def update(self, name, revision, mutate):
        with self.lock:
            previous = self.documents[name]
            if revision != previous.revision:
                raise RevisionConflict("設定が更新されています。再取得してください")
            candidate = copy.deepcopy(dump(previous))
            mutate(candidate)
            candidate["revision"] = revision + 1
            checked = DOCUMENTS[name].model_validate(candidate)
            path = self.root / f"{name}.json"
            # Back up the known-good RAM snapshot, never potentially corrupted external bytes.
            atomic_write(path.with_suffix(".json.bak"), self._encode(dump(previous)))
            atomic_write(path, self._encode(dump(checked)))
            self.documents[name] = checked
            return checked.model_copy(deep=True)


def merge(target: dict, patch: dict):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = value
