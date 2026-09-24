import re
from dataclasses import dataclass, field


def clean_text(text: str, *, skip_urls=True, skip_codeblocks=True) -> str:
    if skip_codeblocks:
        text = re.sub(r"```.*?(?:```|$)", " ", text, flags=re.S)
    if skip_urls:
        text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"<a?:([A-Za-z0-9_]+):\d+>", r"\1", text)
    return " ".join(text.split())


def split_text(text: str, limit: int, max_segments: int) -> tuple[list[str], bool]:
    result = []
    while text and len(result) < max_segments:
        if len(text) <= limit:
            result.append(text)
            text = ""
            break
        boundary = max(text.rfind(c, 0, limit) for c in "。！？.!?\n") + 1
        end = boundary or limit
        result.append(text[:end])
        text = text[end:].lstrip()
    return result, bool(text)


@dataclass
class Message:
    guild_id: str
    channel_id: str
    user_id: str
    text: str
    received_at: float
    message_ids: list[str] = field(default_factory=list)


class Aggregator:
    """Fixed first-message deadline; intervening speakers flush the previous group."""

    def __init__(self, max_chars=200):
        self.max_chars = max_chars
        self.pending: dict[str, tuple[Message, float]] = {}

    def push(self, message: Message, window_ms: int) -> list[Message]:
        ready = self.flush_due(message.received_at)
        previous = self.pending.get(message.guild_id)
        if previous:
            item, deadline = previous
            combined = f"{item.text}、{message.text}"
            if (item.user_id, item.channel_id) == (message.user_id, message.channel_id) and len(
                combined
            ) <= self.max_chars:
                item.text = combined
                item.message_ids.extend(message.message_ids)
                return ready
            ready.append(self.pending.pop(message.guild_id)[0])
        if not window_ms or len(message.text) >= self.max_chars:
            ready.append(message)
        else:
            self.pending[message.guild_id] = (message, message.received_at + window_ms / 1000)
        return ready

    def flush_due(self, now: float) -> list[Message]:
        ready = []
        for guild_id, (message, deadline) in list(self.pending.items()):
            if now >= deadline:
                ready.append(message)
                del self.pending[guild_id]
        return ready

    def clear(self, guild_id: str):
        self.pending.pop(guild_id, None)
