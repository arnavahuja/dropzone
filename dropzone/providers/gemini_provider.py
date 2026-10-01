"""Google Gemini adapter (google-genai SDK)."""

from __future__ import annotations

import uuid
from typing import Any

from .base import LLMProvider, Message, ProviderError, Reply, ToolCall, prune_schema

# Gemini's function-declaration schema is a strict subset and rejects extras.
ALLOWED_SCHEMA_KEYS = {
    "type", "properties", "required", "description", "enum", "items",
    "minimum", "maximum", "nullable",
}


class GeminiProvider(LLMProvider):
    """Gemini models via generate_content with function declarations."""

    name = "gemini"

    def __init__(self, model: str, api_key: str) -> None:
        from google import genai

        self.model = model
        self._genai = genai
        self._client = genai.Client(api_key=api_key)

    def _tools(self, tools: list[dict[str, Any]]) -> list[Any]:
        from google.genai import types

        declarations = [
            types.FunctionDeclaration(
                name=spec["name"],
                description=spec["description"],
                parameters=prune_schema(spec["parameters"], ALLOWED_SCHEMA_KEYS) or None,
            )
            for spec in tools
        ]
        return [types.Tool(function_declarations=declarations)]

    def _contents(self, messages: list[Message]) -> list[Any]:
        from google.genai import types

        contents: list[Any] = []
        for entry in messages:
            role = entry["role"]
            if role == "user":
                contents.append(
                    types.Content(role="user", parts=[types.Part(text=entry["content"])])
                )
            elif role == "assistant":
                parts: list[Any] = []
                if entry.get("content"):
                    parts.append(types.Part(text=entry["content"]))
                for call in entry.get("tool_calls", []):
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(
                                name=call["name"], args=call["args"]
                            )
                        )
                    )
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
            elif role == "tool":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    name=entry["name"],
                                    response=_wrap(entry["result"]),
                                )
                            )
                        ],
                    )
                )
        return contents

    async def complete(
        self, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> Reply:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=self._tools(tools),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=self._contents(messages),
                config=config,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"gemini request failed: {exc}") from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        candidates = response.candidates or []
        if candidates and candidates[0].content and candidates[0].content.parts:
            for part in candidates[0].content.parts:
                if getattr(part, "text", None):
                    text_parts.append(part.text)
                call = getattr(part, "function_call", None)
                if call is not None and call.name:
                    calls.append(
                        ToolCall(
                            id=getattr(call, "id", None) or f"gemini-{uuid.uuid4().hex[:8]}",
                            name=call.name,
                            args=dict(call.args or {}),
                        )
                    )
        return Reply(text="\n".join(text_parts).strip() or None, tool_calls=calls)


def _wrap(result: Any) -> dict[str, Any]:
    """Gemini wants a function response to be an object."""
    return result if isinstance(result, dict) else {"result": result}
