"""Dropzone HTTP server.

    uv run uvicorn app:app --reload

Sessions live in memory, keyed by UUID, and are fully isolated from one another.
Nothing in this file serves `data/drops.json`, the session's coordinates, or the
country code: the only static files served are those in `static/`.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dropzone import agent, game, tools
from dropzone.providers import ProviderError, get_provider, provider_status
from dropzone.session import DIFFICULTIES, MODES, GameSession

load_dotenv()

UTC = dt.timezone.utc
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Sessions are in-memory and expire so a long-running server does not grow
# without bound.
SESSION_TTL = dt.timedelta(hours=12)
MAX_SESSIONS = 200

app = FastAPI(title="Dropzone", version="0.1.0")

SESSIONS: dict[str, GameSession] = {}
_LAST_SEEN: dict[str, dt.datetime] = {}


class NewGameRequest(BaseModel):
    """Body of `POST /new`."""

    mode: str = Field(default="locate", description="locate, escape or expedition")
    difficulty: str = Field(
        default="easy",
        description="easy (name the city) or hard (name the neighbourhood)",
    )
    seed: int | None = Field(default=None, description="Optional RNG seed, for testing")


class ChatRequest(BaseModel):
    """Body of `POST /chat`."""

    message: str
    session_id: str


class SessionRequest(BaseModel):
    """Body of the endpoints that only need to name a session."""

    session_id: str


def _reap() -> None:
    """Forget stale sessions, oldest first if we are over the cap."""
    now = dt.datetime.now(UTC)
    for sid, seen in list(_LAST_SEEN.items()):
        if now - seen > SESSION_TTL:
            SESSIONS.pop(sid, None)
            _LAST_SEEN.pop(sid, None)
    while len(SESSIONS) > MAX_SESSIONS:
        oldest = min(_LAST_SEEN, key=lambda key: _LAST_SEEN[key])
        SESSIONS.pop(oldest, None)
        _LAST_SEEN.pop(oldest, None)


def _touch(session_id: str) -> None:
    """Mark a session as recently used."""
    _LAST_SEEN[session_id] = dt.datetime.now(UTC)


def _get_session(session_id: str) -> GameSession:
    """Look up a session or 404."""
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="that session is gone (expired, or the server restarted). Start a new run.",
        )
    _touch(session_id)
    return session


@app.post("/new")
async def new_game(request: NewGameRequest) -> dict[str, Any]:
    """Start a game. Returns a session id and the opening narration."""
    mode = request.mode.strip().lower()
    if mode not in MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {', '.join(MODES)}")
    difficulty = request.difficulty.strip().lower()
    if difficulty not in DIFFICULTIES:
        raise HTTPException(
            status_code=400, detail=f"difficulty must be one of {', '.join(DIFFICULTIES)}"
        )

    try:
        get_provider()
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _reap()
    try:
        session = await game.new_game(mode, seed=request.seed, difficulty=difficulty)
    except Exception as exc:  # noqa: BLE001 - surface setup failures as 503
        raise HTTPException(status_code=503, detail=f"could not prepare a drop: {exc}") from exc

    SESSIONS[session.session_id] = session
    _touch(session.session_id)

    return {
        "session_id": session.session_id,
        "response": game.opening_text(session),
        "tool_calls": [],
        "status": session.status(),
    }


@app.post("/chat")
async def chat(request: ChatRequest) -> dict[str, Any]:
    """Send the player's message to the narrator and return its reply."""
    session = _get_session(request.session_id)
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message must not be empty")

    try:
        provider = get_provider()
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    turn = await agent.take_turn(session, provider, message)

    # The course requires these three keys, unchanged. `status` is extra.
    return {
        "response": turn["response"],
        "session_id": session.session_id,
        "tool_calls": turn["tool_calls"],
        "status": session.status(),
    }


@app.post("/result")
async def result(request: SessionRequest) -> dict[str, Any]:
    """The official result of a finished run, including the reveal.

    Returns `result: null` while the run is still live, so polling this cannot be
    used to extract the location early.
    """
    session = _get_session(request.session_id)
    return {
        "session_id": session.session_id,
        "status": session.status(),
        "result": session.result_payload if session.game_over else None,
    }


@app.post("/resign")
async def resign(request: SessionRequest) -> dict[str, Any]:
    """End a run early and reveal the result."""
    session = _get_session(request.session_id)
    result = agent.end_now(session, "resigned")
    return {
        "response": "You stop walking and call it in.",
        "session_id": session.session_id,
        "tool_calls": [],
        "status": session.status(),
        "result": result,
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    """Configuration and liveness check. Reveals no key material."""
    return {
        "ok": True,
        "llm": provider_status(),
        "tools": sorted(tools.REGISTRY),
        "drops_available": len(game.load_drops()),
        "active_sessions": len(SESSIONS),
    }


@app.get("/")
async def index() -> FileResponse:
    """The field terminal."""
    return FileResponse(STATIC_DIR / "index.html")


# Mounted last so it cannot shadow the API routes above. Only `static/` is
# served: the seed file lives in `data/`, which is never mounted.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    # Cloud Run injects PORT and requires the server to listen on every
    # interface; locally neither is true, so both are overridable and the
    # defaults stay local-only. Reload is opt-in: it forks a reloader process,
    # which would give each worker its own session store.
    uvicorn.run(
        "app:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "").lower() in ("1", "true", "yes"),
    )
