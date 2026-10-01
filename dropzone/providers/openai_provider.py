"""OpenAI adapter (Chat Completions)."""

from __future__ import annotations

import json
from typing import Any

from .base import LLMProvider, Message, ProviderError, Reply, ToolCall, prune_schema

ALLOWED_SCHEMA_KEYS = {
    "type", "properties", "required", "description", "enum", "items",
    "minimum", "maximum", "minItems", "maxItems", "default",
}


class OpenAIProvider(LLMProvider):
    """GPT models via chat completions with function tools."""

    name = "openai"

    def __init__(self, model: str, api_key: str, base_url: str | None = None) -> None:
        from openai import AsyncOpenAI

        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)

    def _tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": prune_schema(spec["parameters"], ALLOWED_SCHEMA_KEYS),
                },
            }
            for spec in tools
        ]

    def _messages(self, system: str, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for entry in messages:
            role = entry["role"]
            if role == "user":
                out.append({"role": "user", "content": entry["content"]})
            elif role == "assistant":
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": entry.get("content") or None,
                }
                calls = entry.get("tool_calls", [])
                if calls:
                    message["tool_calls"] = [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call["args"], ensure_ascii=False),
                            },
                        }
                        for call in calls
                    ]
                out.append(message)
            elif role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": entry["id"],
                    "content": json.dumps(entry["result"], ensure_ascii=False),
                })
        return out

    async def complete(
        self, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> Reply:
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=self._messages(system, messages),
                tools=self._tools(tools),
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"openai request failed: {exc}") from exc

        choice = response.choices[0].message
        calls: list[ToolCall] = []
        for call in choice.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=call.id, name=call.function.name, args=args))
        return Reply(text=(choice.content or None), tool_calls=calls)
