"""The Run Thread as a text stream: what a Run says becomes messages in its thread.

    run_film(..., out=SlackThread(client, channel, thread_ts))

The pipeline already reports its progress with print(..., file=out, flush=True), so
each flush posts what was written since the last one as one thread message.
"""

from __future__ import annotations

import io
from typing import Any


class SlackThread(io.TextIOBase):
    def __init__(self, client: Any, channel: str, thread_ts: str) -> None:
        super().__init__()
        self.client, self.channel, self.thread_ts = client, channel, thread_ts
        self._buffer: list[str] = []

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._buffer.append(text)
        return len(text)

    def flush(self) -> None:
        text, self._buffer = "".join(self._buffer).strip(), []
        if text:
            self.client.chat_postMessage(channel=self.channel, thread_ts=self.thread_ts, text=text)
