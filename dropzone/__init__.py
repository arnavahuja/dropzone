"""Dropzone: a tool-calling geography survival game.

Layout:
  session.py   server-side truth and per-game state
  game.py      drop selection, extraction placement, the clock
  tools.py     the tools the model may call
  agent.py     the tool-calling loop
  providers/   one adapter per LLM provider behind a shared interface
"""

__all__ = ["agent", "game", "session", "tools"]
