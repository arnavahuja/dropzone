"""The agent loop: send history, run tools, repeat until the model talks.

Plain loop, plain functions, no framework. The loop also enforces the rules the
model cannot be trusted with: it is the loop that notices the clock has run out
and the loop that calls the scorer.
"""

from __future__ import annotations

import json
from typing import Any

from . import scoring, tools
from .prompts import system_prompt
from .providers import LLMProvider, ProviderError
from .session import GameSession

MAX_TOOL_ROUNDS = 8
MAX_HISTORY_ENTRIES = 80
MAX_RESULT_CHARS = 6000


async def take_turn(
    session: GameSession, provider: LLMProvider, message: str
) -> dict[str, Any]:
    """Run one player turn.

    Returns `{"response", "tool_calls", "ended"}`. The caller adds the session id
    and status to build the HTTP response.
    """
    session.history.append({"role": "user", "content": message})
    _trim(session)

    field_log: list[dict[str, Any]] = []
    schemas = tools.all_schemas()

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            reply = await provider.complete(system_prompt(session), session.history, schemas)
        except ProviderError as exc:
            session.history.pop()  # keep a failed turn out of the history
            return {
                "response": f"[radio static] The narrator is unreachable: {exc}",
                "tool_calls": field_log,
                "ended": None,
            }

        session.history.append({
            "role": "assistant",
            "content": reply.text,
            "tool_calls": [
                {"id": call.id, "name": call.name, "args": call.args}
                for call in reply.tool_calls
            ],
        })

        if not reply.wants_tools:
            return {
                "response": reply.text or "[the radio hisses, then nothing]",
                "tool_calls": field_log,
                "ended": session.game_over or None,
            }

        ended_payload: dict[str, Any] | None = None
        for call in reply.tool_calls:
            result = await tools.call_tool(session, call.name, call.args)
            result = _clip(result)
            session.history.append({
                "role": "tool",
                "id": call.id,
                "name": call.name,
                "result": result,
            })
            field_log.append({
                "name": call.name,
                "args": call.args,
                "result": result,
                "game_time": session.watch_time(),
            })
            if isinstance(result, dict) and isinstance(result.get("result"), dict):
                ended_payload = result["result"]  # submit_guess ended the run

        ending = _check_end(session)
        if ending is not None:
            ended_payload = ending
        if ended_payload is not None:
            session.history.append({
                "role": "user",
                "content": (
                    "[game master] The run has ended. Here is the official result, "
                    "which you must report exactly as given, including the revealed "
                    "location and the score:\n"
                    + json.dumps(ended_payload, ensure_ascii=False, indent=1)
                    + "\nNarrate the ending in a few sentences. Call no further tools."
                ),
            })

        _trim(session)

    # Eight rounds without the model saying anything to the player.
    return {
        "response": (
            "[you lose the thread for a moment, head down over the compass] "
            "Say that again, more simply?"
        ),
        "tool_calls": field_log,
        "ended": session.game_over or None,
    }


def _check_end(session: GameSession) -> dict[str, Any] | None:
    """Server-side end-of-game detection. The model has no say in this."""
    if session.game_over:
        return None
    if session.out_of_time():
        return scoring.finalise(session, "out_of_time")
    if session.mode == "escape" and session.extraction_reached:
        return scoring.finalise(session, "extracted")
    if (
        session.mode == "expedition"
        and session.extraction_reached
        and session.guesses_left() <= 0
    ):
        return scoring.finalise(session, "extracted")
    return None


def end_now(session: GameSession, reason: str = "resigned") -> dict[str, Any]:
    """End a run from outside the loop (the player gives up, or asks to stop)."""
    if session.game_over:
        return session.result_payload or {"outcome": session.outcome, "score": session.score}
    return scoring.finalise(session, reason)


def _clip(result: dict[str, Any]) -> dict[str, Any]:
    """Keep a pathological tool result from swamping the context window."""
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) <= MAX_RESULT_CHARS:
        return result
    return {
        "truncated": True,
        "note": "Result was too large and has been shortened.",
        "preview": encoded[:MAX_RESULT_CHARS],
    }


def _trim(session: GameSession) -> None:
    """Drop the oldest history, never splitting a tool call from its result."""
    if len(session.history) <= MAX_HISTORY_ENTRIES:
        return
    cut = len(session.history) - MAX_HISTORY_ENTRIES
    while cut < len(session.history) and session.history[cut]["role"] == "tool":
        cut += 1
    session.history = session.history[cut:]
