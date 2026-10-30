"""The Run Thread as a text stream: what a Run says becomes messages in its thread.

    run_film(..., out=SlackThread(client, channel, thread_ts))

The pipeline already reports its progress with print(..., file=out, flush=True), so
each flush posts what was written since the last one as one thread message.

Posting is best-effort: a Slack error is logged, never raised, because the pipeline's
progress lines must not be able to cost a paid Run its Run Log or its decisions. The
text is escaped (it can carry model-written posts) and cut short if it's too long.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from trailer_to_campaign.slack_approval import escape

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 3_500  # well under Slack's limit, and still readable in a thread


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
        text, self._buffer = escape("".join(self._buffer).strip()), []
        if not text:
            return
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "\n… (cut short here; the Run Log has the rest)"
        try:
            self.client.chat_postMessage(channel=self.channel, thread_ts=self.thread_ts, text=text)
        except Exception:
            log.warning("could not post to the Run Thread %s", self.thread_ts, exc_info=True)
