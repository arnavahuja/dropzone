"""Anthropic adapter."""

from __future__ import annotations

import json
from typing import Any

from .base import LLMProvider, Message, ProviderError, Reply, ToolCall, prune_schema

ALLOWED_SCHEMA_KEYS = {
    "type", "properties", "required", "description", "enum", "items",
    "minimum", "maximum", "minItems", "maxItems", "default",
}


class AnthropicProvider(LLMProvider):
    """Claude via the Messages API."""

    name = "anthropic"

    def __init__(self, model: str, api_key: str, max_tokens: int = 1400) -> None:
        import anthropic

        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    def _tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": spec["name"],
                "description": spec["description"],
                "input_schema": prune_schema(spec["parameters"], ALLOWED_SCHEMA_KEYS),
            }
            for spec in tools
        ]

    def _messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Fold neutral history into Anthropic's alternating-role blocks."""
        out: list[dict[str, Any]] = []
        for entry in messages:
            role = entry["role"]
            if role == "user":
                out.append({"role": "user", "content": entry["content"]})
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if entry.get("content"):
                    blocks.append({"type": "text", "text": entry["content"]})
                for call in entry.get("tool_calls", []):
                    blocks.append({
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call["args"],
                    })
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": entry["id"],
                    "content": json.dumps(entry["result"], ensure_ascii=False),
                }
                # Consecutive tool results belong in one user message.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    async def complete(
        self, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> Reply:
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=self._messages(messages),
                tools=self._tools(tools),
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"anthropic request failed: {exc}") from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, args=dict(block.input or {})))
        return Reply(text="\n".join(text_parts).strip() or None, tool_calls=calls)
