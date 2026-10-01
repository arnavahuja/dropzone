"""The agent loop and the HTTP contract, driven by a scripted fake provider."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module
from dropzone import agent, game, tools
from dropzone.providers import ProviderError
from dropzone.providers.base import Reply, ToolCall
from tests.conftest import make_session, run

pytestmark = pytest.mark.usefixtures("offline")


class ScriptedProvider:
    """Returns a prepared list of replies, one per round, recording what it saw."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def complete(self, system, messages, tools_):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools_})
        if not self.replies:
            return Reply(text="(nothing more to say)")
        return self.replies.pop(0)


def test_loop_runs_a_tool_then_narrates() -> None:
    session = make_session()
    provider = ScriptedProvider(
        Reply(tool_calls=[ToolCall(id="t1", name="look_around", args={"radius_m": 400})]),
        Reply(text="Cobbles underfoot, a tram line to the north."),
    )

    turn = run(agent.take_turn(session, provider, "look around"))

    assert turn["response"].startswith("Cobbles")
    assert len(turn["tool_calls"]) == 1
    entry = turn["tool_calls"][0]
    assert entry["name"] == "look_around"
    assert entry["args"] == {"radius_m": 400}
    assert entry["result"]["features_found"] > 0
    assert "game_time" in entry
    # History keeps the user turn, the assistant turn, the tool result, the text.
    assert [m["role"] for m in session.history] == ["user", "assistant", "tool", "assistant"]


def test_loop_chains_tools_across_rounds() -> None:
    session = make_session()
    provider = ScriptedProvider(
        Reply(tool_calls=[ToolCall(id="t1", name="look_around", args={"radius_m": 500})]),
        Reply(tool_calls=[ToolCall(id="t2", name="read_sign", args={"feature_id": "F003"})]),
        Reply(text="Latin letters, an accent on the second word."),
    )

    turn = run(agent.take_turn(session, provider, "read the nearest sign"))

    assert [c["name"] for c in turn["tool_calls"]] == ["look_around", "read_sign"]
    assert len(provider.calls) == 3


def test_loop_passes_tool_schemas_and_a_redacted_prompt() -> None:
    session = make_session()
    provider = ScriptedProvider(Reply(text="Nothing moves."))
    run(agent.take_turn(session, provider, "hello"))

    sent = provider.calls[0]
    assert len(sent["tools"]) == len(tools.REGISTRY)
    assert {"name", "description", "parameters"} == set(sent["tools"][0])
    assert "38.7" not in sent["system"] and "Portugal" not in sent["system"]


def test_loop_stops_after_eight_tool_rounds() -> None:
    session = make_session()
    looping = [
        Reply(tool_calls=[ToolCall(id=f"t{i}", name="check_status", args={})])
        for i in range(20)
    ]
    provider = ScriptedProvider(*looping)

    turn = run(agent.take_turn(session, provider, "keep going"))

    assert len(provider.calls) == agent.MAX_TOOL_ROUNDS
    assert "Say that again" in turn["response"]


def test_provider_failure_is_reported_not_raised() -> None:
    class Broken:
        name, model = "broken", "none"

        async def complete(self, system, messages, tools_):
            raise ProviderError("401 unauthorized")

    session = make_session()
    turn = run(agent.take_turn(session, Broken(), "look around"))

    assert "narrator is unreachable" in turn["response"]
    assert "401" in turn["response"]
    assert session.history == [], "a failed turn should leave no history behind"


def test_running_out_of_time_ends_the_run_server_side() -> None:
    session = make_session(mode="locate", minutes=4)
    provider = ScriptedProvider(
        Reply(tool_calls=[ToolCall(id="t1", name="look_around", args={"radius_m": 500})]),
        Reply(text="The light goes and the radio crackles."),
    )

    turn = run(agent.take_turn(session, provider, "look around"))

    assert session.game_over is True
    assert session.ended_reason == "out_of_time"
    assert session.result_payload["outcome"] == "failure"
    # The model is handed the official result to read back.
    handoff = [m for m in session.history if m["role"] == "user" and "[game master]" in str(m["content"])]
    assert handoff and "true_location" in handoff[0]["content"]
    assert turn["ended"] is True


def test_reaching_extraction_ends_an_escape_run() -> None:
    session = make_session(mode="escape")
    provider = ScriptedProvider(
        Reply(tool_calls=[ToolCall(id="t1", name="move", args={"direction": "north", "distance_m": 1500})]),
        Reply(tool_calls=[ToolCall(id="t2", name="move", args={"direction": "north", "distance_m": 1500})]),
        Reply(text="A windsock, and a flattened patch of grass."),
    )

    run(agent.take_turn(session, provider, "walk north until you hit it"))

    assert session.extraction_reached is True
    assert session.game_over is True
    assert session.result_payload["outcome"] == "success"
    assert session.result_payload["score"] > 600


def test_history_is_trimmed_without_orphaning_tool_results() -> None:
    session = make_session()
    for i in range(agent.MAX_HISTORY_ENTRIES + 20):
        session.history.append({"role": "tool", "id": str(i), "name": "check_status", "result": {}})
    session.history.append({"role": "user", "content": "status"})

    agent._trim(session)

    assert len(session.history) <= agent.MAX_HISTORY_ENTRIES + 1
    assert session.history[0]["role"] != "tool"


def test_oversized_tool_results_are_clipped() -> None:
    clipped = agent._clip({"blob": "x" * (agent.MAX_RESULT_CHARS + 100)})
    assert clipped["truncated"] is True
    assert len(clipped["preview"]) == agent.MAX_RESULT_CHARS


# --- HTTP ------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    """A TestClient whose provider is scripted and whose drops are local."""
    provider = ScriptedProvider()

    def fake_get_provider():
        return provider

    monkeypatch.setattr(app_module, "get_provider", fake_get_provider)
    app_module.SESSIONS.clear()
    with TestClient(app_module.app) as test_client:
        test_client.provider = provider  # type: ignore[attr-defined]
        yield test_client


def test_new_returns_a_session_and_an_opening(client) -> None:
    response = client.post("/new", json={"mode": "locate", "seed": 7})
    assert response.status_code == 200
    body = response.json()

    assert body["session_id"]
    assert len(body["response"]) > 50
    assert body["tool_calls"] == []
    assert body["status"]["guesses_left"] == 3
    # The opening must not name the place it dropped you in.
    session = app_module.SESSIONS[body["session_id"]]
    assert session.reveal_label.split(",")[0] not in body["response"]


def test_new_rejects_an_unknown_mode(client) -> None:
    assert client.post("/new", json={"mode": "sightseeing"}).status_code == 400


def test_chat_returns_the_required_shape(client) -> None:
    session_id = client.post("/new", json={"mode": "locate", "seed": 7}).json()["session_id"]
    client.provider.replies = [
        Reply(tool_calls=[ToolCall(id="t1", name="check_weather", args={})]),
        Reply(text="Mild, broken cloud, a breeze off the water."),
    ]

    body = client.post("/chat", json={"message": "what's the weather", "session_id": session_id}).json()

    assert {"response", "session_id", "tool_calls"} <= set(body)
    assert body["session_id"] == session_id
    assert isinstance(body["response"], str)
    call = body["tool_calls"][0]
    assert {"name", "args", "result"} <= set(call)
    assert call["name"] == "check_weather"
    assert isinstance(call["result"], dict)


def test_chat_rejects_an_unknown_session(client) -> None:
    response = client.post("/chat", json={"message": "hi", "session_id": "nope"})
    assert response.status_code == 404


def test_chat_rejects_an_empty_message(client) -> None:
    session_id = client.post("/new", json={"mode": "locate", "seed": 7}).json()["session_id"]
    assert client.post("/chat", json={"message": "   ", "session_id": session_id}).status_code == 400


def test_result_is_withheld_until_the_run_ends(client) -> None:
    session_id = client.post("/new", json={"mode": "locate", "seed": 7}).json()["session_id"]

    assert client.post("/result", json={"session_id": session_id}).json()["result"] is None

    body = client.post("/resign", json={"session_id": session_id}).json()
    assert body["result"]["outcome"] == "failure"
    assert body["result"]["true_location"]
    # Once over, the stored payload is served, not a fresh scoring run.
    again = client.post("/result", json={"session_id": session_id}).json()["result"]
    assert again == body["result"]


def test_health_reports_configuration_without_secrets(client) -> None:
    body = client.get("/health").json()
    assert body["ok"] is True
    assert len(body["tools"]) == 14
    assert body["drops_available"] >= 80
    assert "api_key" not in str(body).replace("api_key_present", "")


def test_index_is_served(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "DROPZONE" in response.text


def test_the_seed_file_is_not_served(client) -> None:
    for path in ("/data/drops.json", "/static/../data/drops.json", "/drops.json"):
        assert client.get(path).status_code in (403, 404)


def test_sessions_are_separate_over_http(client) -> None:
    first = client.post("/new", json={"mode": "escape", "seed": 1}).json()["session_id"]
    second = client.post("/new", json={"mode": "escape", "seed": 2}).json()["session_id"]
    assert first != second

    client.provider.replies = [
        Reply(tool_calls=[ToolCall(id="t1", name="move", args={"direction": "south", "distance_m": 2000})]),
        Reply(text="Downhill, and the air gets warmer."),
    ]
    client.post("/chat", json={"message": "walk south", "session_id": first})

    moved = client.post("/result", json={"session_id": first}).json()["status"]
    still = client.post("/result", json={"session_id": second}).json()["status"]
    assert moved["offset_from_drop"] != "at the drop point"
    assert still["offset_from_drop"] == "at the drop point"


# --- drop selection --------------------------------------------------------


def test_seed_file_covers_the_inhabited_world() -> None:
    drops = game.load_drops()
    assert len(drops) >= 80

    settings = {drop["setting"] for drop in drops}
    assert {"city", "village", "trailhead", "coast", "farmland"} <= settings

    lons = [drop["lon"] for drop in drops]
    lats = [drop["lat"] for drop in drops]
    assert min(lons) < -70 and max(lons) > 140      # Americas through Oceania
    assert min(lats) < -30 and max(lats) > 55       # southern Australasia to Nordic
    for drop in drops:
        assert -90 <= drop["lat"] <= 90 and -180 <= drop["lon"] <= 180


def test_new_game_jitters_the_seed_and_sets_a_clock() -> None:
    session = run(game.new_game("expedition", seed=11))

    assert session.country_code == "PT"           # from the stubbed reverse geocode
    assert session.extraction_lat is not None
    assert 180 <= session.total_minutes <= 360
    assert session.minutes_remaining == session.total_minutes

    from dropzone import geo

    extraction_distance = geo.haversine_m(
        session.drop_lat, session.drop_lon, session.extraction_lat, session.extraction_lon
    )
    assert 1500 <= extraction_distance <= 6000

    # The jittered drop is near a seed but not exactly on one.
    nearest = min(
        geo.haversine_m(session.drop_lat, session.drop_lon, d["lat"], d["lon"])
        for d in game.load_drops()
    )
    assert nearest <= 320


def test_locate_mode_has_no_extraction_point() -> None:
    session = run(game.new_game("locate", seed=3))
    assert session.extraction_lat is None
    assert session.mode == "locate"


def test_a_night_drop_starts_in_the_morning() -> None:
    import datetime as dt

    from dropzone import game as game_module

    # Midnight UTC over Japan is the middle of the night there.
    start, deadline = game_module.plan_clock(35.7, 139.8, dt.datetime(2026, 10, 1, 18, 0, tzinfo=dt.timezone.utc))
    assert deadline > start
    assert (deadline - start).total_seconds() / 60 >= 180
    from dropzone import sun

    assert sun.is_daylight(35.7, 139.8, start)
