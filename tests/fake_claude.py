"""A scripted stand-in for the Claude API client, shared by the agent tests.

It answers each messages.create() with the next scripted step and records every
request (and every count_tokens call) as it was sent, so a test sees exactly what
the model would see. Nothing reaches the network or costs money.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from anthropic.types import Message

COUNTED_TOKENS = 3_000


Step = Callable[[dict[str, Any]], Message]


class FakeMessages:
    """Answers each create() with the next scripted step, and records every request."""

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        self.counted: list[dict[str, Any]] = []

    async def count_tokens(self, **params: Any) -> Any:
        self.counted.append({**params, "messages": list(params["messages"])})
        return type("Count", (), {"input_tokens": COUNTED_TOKENS})()

    async def create(self, **params: Any) -> Message:
        self.requests.append({**params, "messages": list(params["messages"])})
        if not self.steps:
            raise AssertionError("the loop asked for more turns than scripted")
        return self.steps.pop(0)(params)


class FakeClient:
    def __init__(self, steps: list[Step]) -> None:
        self.messages = FakeMessages(steps)


def reply(*content: dict[str, Any], stop: str = "tool_use") -> Message:
    return Message.model_validate(
        {
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": list(content),
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 2_000, "output_tokens": 300},
        }
    )


def use(name: str, n: int = 1, **arguments: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"tu_{name}_{n}", "name": name, "input": arguments}


THINKING = {"type": "thinking", "thinking": "", "signature": "sig-1"}


def results(params: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool_result blocks the loop sent back in this request."""
    return params["messages"][-1]["content"]


def result_json(params: dict[str, Any], tool_use_id: str) -> Any:
    [block] = [b for b in results(params) if b["tool_use_id"] == tool_use_id]
    assert not block["is_error"], block["content"]
    return json.loads(block["content"][0]["text"])
