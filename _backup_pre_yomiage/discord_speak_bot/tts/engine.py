from typing import Protocol

from ..pipeline.scheduler import SpeechJob


class Engine(Protocol):
    def load(self) -> None: ...
    def synthesize(self, job: SpeechJob) -> bytes: ...
    def unload(self) -> None: ...


class FakeEngine:
    """Explicit development mode, produces silence and never downloads a model."""

    def load(self):
        pass

    def synthesize(self, job):
        return bytes(48000 * 2 * 2 // 5)

    def unload(self):
        pass
