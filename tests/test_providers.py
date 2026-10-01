"""Provider selection and history translation.

The adapters are exercised without their SDKs or the network: the translation
from neutral history into each provider's wire format is pure, so it is built on
instances created with `object.__new__`.
"""

from __future__ import annotations

import json

import pytest

from dropzone import providers
from dropzone.providers.anthropic_provider import AnthropicProvider
from dropzone.providers.base import ProviderError, prune_schema
from dropzone.providers.openai_provider import OpenAIProvider

NEUTRAL = [
    {"role": "user", "content": "look around"},
    {
        "role": "assistant",
        "content": "Let me check.",
        "tool_calls": [{"id": "t1", "name": "look_around", "args": {"radius_m": 500}}],
    },
    {"role": "tool", "id": "t1", "name": "look_around", "result": {"features_found": 9}},
    {"role": "user", "content": "and the sun?"},
]


# --- configuration ---------------------------------------------------------


def test_missing_provider_is_explained(monkeypatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    with pytest.raises(ProviderError, match="LLM_PROVIDER is not set"):
        providers.get_provider()


def test_unknown_provider_lists_the_valid_ones(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "llamafile")
    with pytest.raises(ProviderError, match="unknown LLM_PROVIDER"):
        providers.get_provider()


def test_missing_model_is_explained(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(ProviderError, match="LLM_MODEL is not set"):
        providers.get_provider()


def test_missing_key_names_the_variable(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        providers.get_provider()


def test_no_model_name_is_hardcoded() -> None:
    """Models come from LLM_MODEL only, so none may be baked into the code."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    patterns = ("claude-", "gpt-4", "gpt-5", "gemini-1", "gemini-2", "o3-", "o4-")
    for path in list(root.glob("dropzone/**/*.py")) + [root / "app.py"]:
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            assert pattern not in text, f"{path.name} hardcodes a model name ({pattern})"


def test_provider_status_hides_the_key(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_MODEL", "a-model")
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret")
    status = providers.provider_status()
    assert status == {"provider": "gemini", "model": "a-model", "api_key_present": True}
    assert "super-secret" not in json.dumps(status)


# --- schema pruning --------------------------------------------------------


def test_prune_schema_drops_unsupported_keywords() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "radius_m": {"type": "integer", "minimum": 100, "examples": [500]},
            "tags": {"type": "array", "items": {"type": "string", "pattern": "^a"}},
        },
        "required": ["radius_m"],
    }
    pruned = prune_schema(schema, {"type", "properties", "required", "items", "minimum"})

    assert "additionalProperties" not in pruned
    assert "examples" not in pruned["properties"]["radius_m"]
    assert pruned["properties"]["radius_m"]["minimum"] == 100
    assert pruned["properties"]["tags"]["items"] == {"type": "string"}
    assert pruned["required"] == ["radius_m"]


# --- translation -----------------------------------------------------------


def test_anthropic_translation_folds_tool_results_into_user_blocks() -> None:
    adapter = object.__new__(AnthropicProvider)
    messages = adapter._messages(NEUTRAL)

    assert [m["role"] for m in messages] == ["user", "assistant", "user", "user"]
    assistant = messages[1]["content"]
    assert assistant[0]["type"] == "text"
    assert assistant[1] == {
        "type": "tool_use", "id": "t1", "name": "look_around", "input": {"radius_m": 500},
    }
    tool_result = messages[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "t1"
    assert json.loads(tool_result["content"]) == {"features_found": 9}


def test_anthropic_consecutive_tool_results_share_one_message() -> None:
    adapter = object.__new__(AnthropicProvider)
    history = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "name": "listen", "args": {}},
            {"id": "b", "name": "read_sun", "args": {}},
        ]},
        {"role": "tool", "id": "a", "name": "listen", "result": {"sounds": []}},
        {"role": "tool", "id": "b", "name": "read_sun", "result": {"daylight": True}},
    ]
    messages = adapter._messages(history)
    assert len(messages) == 2
    assert len(messages[1]["content"]) == 2


def test_anthropic_tools_use_input_schema() -> None:
    adapter = object.__new__(AnthropicProvider)
    tools = adapter._tools([
        {"name": "listen", "description": "d", "parameters": {"type": "object", "properties": {}}}
    ])
    assert tools[0]["input_schema"] == {"type": "object", "properties": {}}
    assert "parameters" not in tools[0]


def test_openai_translation_uses_function_tool_calls() -> None:
    adapter = object.__new__(OpenAIProvider)
    messages = adapter._messages("you are the narrator", NEUTRAL)

    assert messages[0] == {"role": "system", "content": "you are the narrator"}
    assistant = messages[2]
    assert assistant["tool_calls"][0]["function"]["name"] == "look_around"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"radius_m": 500}
    assert messages[3]["role"] == "tool"
    assert messages[3]["tool_call_id"] == "t1"


def test_openai_tools_are_wrapped_as_functions() -> None:
    adapter = object.__new__(OpenAIProvider)
    tools = adapter._tools([
        {"name": "listen", "description": "d", "parameters": {"type": "object", "properties": {}}}
    ])
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "listen"


def test_both_adapters_carry_every_tool_through() -> None:
    from dropzone import tools as game_tools

    schemas = game_tools.all_schemas()
    anthropic = object.__new__(AnthropicProvider)._tools(schemas)
    openai = object.__new__(OpenAIProvider)._tools(schemas)

    assert len(anthropic) == len(openai) == len(schemas)
    assert {t["name"] for t in anthropic} == {t["function"]["name"] for t in openai}
