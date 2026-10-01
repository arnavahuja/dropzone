"""The provider-neutral interface the game loop talks to.

The game, the tools and the session never import a provider SDK. They speak this
vocabulary; one adapter per provider translates it. Swapping `LLM_PROVIDER` in
`.env` is therefore the whole of "changing provider".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    """A tool the model wants run."""

    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Reply:
    """One model turn: narration text, tool calls, or both."""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        """True when the loop should execute tools and come back for more."""
        return bool(self.tool_calls)


# Neutral history entries, as stored on the session:
#   {"role": "user", "content": "..."}
#   {"role": "assistant", "content": "...", "tool_calls": [ToolCall-as-dict, ...]}
#   {"role": "tool", "id": "...", "name": "...", "result": {...}}
Message = dict[str, Any]


class LLMProvider(Protocol):
    """What the game loop needs from a model."""

    name: str
    model: str

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> Reply:
        """Send the conversation and return text, tool calls, or both."""
        ...


class ProviderError(RuntimeError):
    """A provider call failed in a way the loop should report, not crash on."""


def prune_schema(schema: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    """Recursively keep only JSON-Schema keywords a provider accepts.

    Gemini in particular rejects unknown keywords outright, so each adapter
    declares what it can take rather than hoping.
    """
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in allowed:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: prune_schema(v, allowed) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[key] = prune_schema(value, allowed)
        else:
            out[key] = value
    return out
