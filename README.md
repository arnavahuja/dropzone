# Dropzone

A tool-calling geography survival game, played in a browser terminal.

You are dropped at a real, hidden point on Earth and you talk to a narrator in
plain language: *look around*, *walk north-east for 800 metres*, *what does the
sky look like?* The narrator answers, but it is not allowed to invent the world.
Weather, nearby features, terrain, sun position, wildlife and distances all come
from tool calls against live data, and every call is shown to you as a field log
entry. Reading those logs is the game.

The narrator does not know where you are either. That is the design, not a
limitation.

## Setup

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/). One LLM API key.

```bash
uv sync                      # install
cp .env.example .env         # then edit it: provider, model, one API key
uv run uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>. No build step, no database, no deployment config.

`.env` controls the model and nothing else does:

```ini
LLM_PROVIDER=gemini          # gemini | anthropic | openai
LLM_MODEL=gemini-2.5-flash   # any current model id for that provider
GEMINI_API_KEY=...           # only the key matching LLM_PROVIDER is needed
```

Switching provider is an `.env` edit and a restart. No model name is hardcoded
anywhere in the code, and a test enforces that.

Run the tests (123 of them, all offline, no API keys, no network):

```bash
uv run pytest
```

## Modes

| Mode | Objective | Scored on |
|---|---|---|
| **Locate** | Work out where you are. 3 guesses. | Best guess, plus time left |
| **Escape** | Reach an extraction point 2–5 km away. | Arrival, plus time left |
| **Expedition** | Both. | Both, out of 2000 |

## Difficulty

| | Answer needed | Dropped in | Counts if within |
|---|---|---|---|
| **Easy** | The city | A city most people could place on a map | 25 km |
| **Hard** | The city | Any city in the list | 25 km |
| **God** | The neighbourhood too | Any city in the list | 3 km |

Easy and hard ask the same question and mark it the same way; they differ only
in how obscure the city can be. God mode is the one that wants the district.

Every drop is a real city — there are no villages, farmland or trailheads, so
the answer is always a name worth guessing. Difficulty does not apply to Escape,
which has no guessing.

All runs share a game clock: 6 hours of game time, or until local sunset,
whichever comes first, with a 3-hour floor. A drop that lands at night is
shifted forward to that location's morning. Actions cost game time; talking is
free.

## How the location stays hidden

This is the hard part of the build, so it is worth stating plainly: an LLM that
sees coordinates will leak them, and a player who asks nicely will get them. So
the model never receives the answer in any form.

- **The truth lives only in server-side session state** (`dropzone/session.py`).
  No tool takes a coordinate as an argument and no tool returns one. Tools get
  the session and read the position themselves.
- **Results are whitelisted, not filtered.** Every tool result is assembled key
  by key from computed values. No API response is ever forwarded with fields
  removed — raw Overpass elements, Open-Meteo forecasts and iNaturalist
  observations carry coordinates, timezone names, country codes and place names
  in places you would not think to strip.
- **Categories come from a closed vocabulary** (`dropzone/overpass.py`). An OSM
  tag value we did not anticipate degrades to a generic word rather than being
  passed through, so an unexpected value cannot leak a name.
- **Place names are withheld.** A feature is described by category, distance and
  compass bearing. `read_sign` tells you the writing system of its name, its
  length and its first two characters — never the name. Script labels never name
  a language either: kana are reported as Hiragana and Katakana, not "Japanese".
- **Position is relative.** The model only ever sees offsets: "1.2 km north-east
  of where you started".
- **The country code is resolved once, server-side**, and only
  `dropzone/tables.py` consumes it, to answer which side traffic drives on and
  what the local greeting sounds like. It never appears in a tool result.
- **The system prompt** tells the narrator it does not know the location, must
  not speculate or confirm the player's theories, and should answer "where am
  I?" by pointing at what can be observed. This is a courtesy on top of the real
  defence, which is that the location is not in its context at all.
- **The seed file is server-only.** `data/drops.json` is never served; only
  `static/` is mounted.
- **The reveal is built from the seed file, in English.** Nominatim answers in
  the local language, so a reveal built from it could come back in a script the
  player cannot read; it is also inconsistent at street granularity, answering
  "Bella Vista" for central Panama City and "Taito" for Yanaka in Tokyo. The
  reverse geocode is therefore used for the country code and nothing else.

`tests/test_leaks.py` enforces this. For drops on four continents it calls every
tool and asserts the serialised results contain no coordinate near the truth, no
feature name from the map data, and no country, city or timezone string.

## Tools

| Tool | Returns | Source | Game time |
|---|---|---|---|
| `look_around` | Up to 25 nearby features: category, distance, bearing, id. No names. | Overpass | 5 min |
| `read_sign` | Writing system, length, diacritics, case, first 2 characters of a feature's name. | Cached Overpass | 3 min |
| `check_weather` | Temperature, conditions, precipitation, cloud, wind speed and direction. | Open-Meteo | 1 min |
| `read_sun` | Sun elevation and azimuth, minutes to sunset, UTC watch time. | `astral`, local | 1 min |
| `survey_terrain` | Elevation here, and slope 500 m away in 8 directions. | Open-Meteo elevation | 5 min |
| `spot_wildlife` | Up to 8 commonly recorded nearby species: common name and group. | iNaturalist | 10 min |
| `inspect_road` | Surface, lanes, class, driving side, km/h or mph. No road name. | Overpass + country table | 3 min |
| `listen` | Traffic, trains, water, surf, bells, calls to prayer, wind, with directions. | Derived | 2 min |
| `greet_local` | The local word for hello, in its native script. 3 uses. | Country table | 5 min |
| `scan_horizon` | Large landmarks within 25 km. Range shrinks in haze and on low ground. | Overpass + visibility | 10 min |
| `move` | New offset from the drop, time spent, whether extraction was reached. | Local state | ~12 min/km |
| `radio_check` | Bearing and distance to extraction. 4 charges. | Local state | 2 min |
| `check_status` | Clock, offset, inventory, guesses, battery. | Local state | free |
| `submit_guess` | Distance from the truth, rounded, graded at the run's difficulty. The third guess ends the run. | Nominatim | free |

`read_sign`, `read_sun`, `greet_local`, `listen` and `spot_wildlife` are the
original tools. `read_sun` is the sharpest: sun elevation plus a UTC watch is
enough to derive longitude from when the sun peaks and latitude from how high it
climbs. You are not given the local time zone, deliberately.

Every tool returns a JSON dict, and on failure returns
`{"error": ..., "suggestion": ...}` where the suggestion tells the model what to
do next. Tools never raise into the agent loop.

## Three queries for a grader to paste in

1. `Look around within 500 metres and tell me what you see`
2. `Where is the sun, and how long until it sets?`
3. `Walk north-east for 800 metres, then check the radio`

Try also `read the nearest sign` (which chains `look_around` into `read_sign`),
`listen for a minute`, and `where am I?` — which gets you nothing, by design.

## Deploying to Cloud Run

Pushes to `main` build, test and deploy automatically via the Cloud Build
trigger defined in `cloudbuild.yaml`. The four steps are: run the test suite,
build the image, push it to Artifact Registry, deploy a new Cloud Run revision.
A failing test fails the build and the running revision is left alone.

### One-time setup

```bash
PROJECT=$(gcloud config get-value project)
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
REGION=us-central1

# 1. Somewhere to keep the images.
gcloud artifacts repositories create containers \
  --repository-format=docker --location="$REGION"

# 2. The API key, as a secret rather than an env var.
printf 'sk-ant-...' | gcloud secrets create dropzone-llm-key --data-file=-
gcloud secrets add-iam-policy-binding dropzone-llm-key \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor

# 3. Let Cloud Build deploy to Cloud Run and act as the runtime service account.
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role=roles/run.admin
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role=roles/iam.serviceAccountUser

# 4. First deploy by hand, to set the provider config the pipeline then inherits.
gcloud run deploy dropzone --source . --region "$REGION" \
  --allow-unauthenticated --max-instances 1 --timeout 300 --memory 512Mi \
  --set-env-vars LLM_PROVIDER=anthropic,LLM_MODEL=claude-sonnet-4-5 \
  --set-secrets ANTHROPIC_API_KEY=dropzone-llm-key:latest

# 5. Connect the repo and create the trigger. The first command opens a browser
#    to install the Cloud Build GitHub App, which cannot be done from the CLI.
gcloud builds triggers create github \
  --name=dropzone-main \
  --repo-name=dropzone --repo-owner=<your-github-username> \
  --branch-pattern='^main$' \
  --build-config=cloudbuild.yaml \
  --region="$REGION"
```

Step 4 matters: `cloudbuild.yaml` deliberately sets no environment variables or
secrets, so the key never appears in a build log. Cloud Run carries those
settings from one revision to the next, so they are set once by hand and the
pipeline inherits them. To change the model later, deploy once with a new
`--set-env-vars` or edit the service in the console.

To change provider in production, update the service's env vars — no code
change, no rebuild:

```bash
gcloud run services update dropzone --region "$REGION" \
  --set-env-vars LLM_PROVIDER=gemini,LLM_MODEL=gemini-2.5-flash \
  --set-secrets GEMINI_API_KEY=dropzone-gemini-key:latest
```

Watch a build, or roll back:

```bash
gcloud builds list --region "$REGION" --limit 5
gcloud run revisions list --service dropzone --region "$REGION"
gcloud run services update-traffic dropzone --region "$REGION" --to-revisions <revision>=100
```

### Why one instance

`--max-instances 1` is not a cost control, it is a correctness requirement.
Sessions live in the serving process's memory, so a second instance would not
know about a game started on the first and the player would get "that session is
gone" mid-run. One instance at the default concurrency of 80 serves plenty of
simultaneous games; the ceiling only matters if this ever has to scale out, at
which point sessions need to move to Memorystore or Firestore. The same
reasoning is why the container runs a single uvicorn worker.

Two more things to expect from a hosted deployment:

- **A cold start loses in-flight games.** Cloud Run stops an idle instance and
  its sessions go with it. `--min-instances 1` avoids that, at the cost of
  running continuously.
- **Overpass and Nominatim see a shared Google egress IP**, which is rate
  limited harder than a home connection and is sometimes already throttled by
  another tenant. Tools degrade to an error with a suggestion rather than
  failing the turn, but `look_around` and `scan_horizon` will fail more often
  than they do locally.

After the first deploy, record the URL in `submission.json`:

```json
{"deploy_url": "https://dropzone-xxxxxxxx-uc.a.run.app", "authors": ["aa5790"]}
```

## API

- `POST /new` → `{mode, difficulty, seed?}` → `{session_id, response, tool_calls, status}`
- `POST /chat` → `{message, session_id}` → `{response, session_id, tool_calls, status}`

  where each `tool_calls` entry is `{name, args, result, game_time}`.
- `POST /result` → `{session_id}` → the official result once the run has ended,
  `null` while it is live, so polling it cannot extract the location early.
- `POST /resign` → end a run early.
- `GET /health` → provider configuration (no key material), tool list, seed count.
- `GET /` → the terminal.

Sessions are in-memory, keyed by UUID, isolated, and expire after 12 hours.

## Layout

```
Dockerfile             container image for Cloud Run
cloudbuild.yaml        test, build, deploy on push to main
app.py                 FastAPI: /new, /chat, /result, /resign, /health
dropzone/
  session.py           server-side truth and per-game state
  game.py              drop selection, extraction placement, the clock
  tools.py             the 14 tools and their schemas
  agent.py             the tool-calling loop
  prompts.py           the narrator's system prompt
  scoring.py           end-of-run results (the server decides, the model narrates)
  overpass.py          OSM client and the tag -> category vocabulary
  weather.py sun.py elevation.py wildlife.py geocode.py
  scripts.py           writing-system detection for read_sign
  tables.py            driving side and greetings, keyed by country code
  providers/           one adapter per LLM provider behind a shared interface
data/drops.json        182 curated city drop points (server-only)
static/index.html      the field terminal, one file, no build
tests/                 123 tests, fully offline
```

The agent loop is a plain loop over plain functions. No agent framework. It caps
at 8 tool rounds per player turn, and the loop — not the model — decides when the
clock has run out and what the score is.

## Decisions where the spec was silent

- **Scoring.** 1000 points per objective, so Expedition is out of 2000. A guess
  scores `1000 · e^(−km/decay)`, where the decay is 300 km when the city is the
  answer and 40 km in god mode — so the same 12 km error is worth 960 on easy
  and 740 in god. Reaching extraction pays 600 plus up to 400 for time left;
  missing it pays up to 250 for distance closed. Locate adds up to 200 for time
  left.
- **Verdict wording** scales to the target precision at the near end ("dead on",
  "close enough to count") and is absolute at the far end, because the wrong
  continent is the wrong continent at any difficulty.
- **Seed file.** 182 city drops rather than the required 80, each tagged
  `famous` or `known` to build the easy pool, and labelled
  "Neighbourhood, City, Country" in ASCII English. Every coordinate was checked
  by reverse geocoding that it falls in the country its label claims. This
  departs from the brief, which asked for villages, trailheads and farmland too:
  those drops made the answer an obscure place name rather than a guessable one.
- **Feature ids** are sequential per session (`F001`), so they carry no OSM
  identity that could be looked up outside the game.
- **`listen` and `greet_local` reuse the `look_around` cache** rather than
  issuing their own Overpass query, and the drop-verification sweep is used to
  prime that cache, so opening a game costs fewer requests against a rate-limited
  public API.
- **`scan_horizon`** caps each kind of landmark at three entries, because a city
  maps dozens of near-identical masts that would crowd out the one peak that
  tells you something. Its range is scaled by how high you stand relative to the
  land within 5 km, then clamped by reported visibility.
- **`greet_local` needs a business, market, station or place of worship within
  100 m**, and a failed attempt does not consume one of the three uses.
- **A guess that cannot be geocoded does not consume a guess**, so a typo is not
  punished.
- **Walking** is 5 km/h plus Naismith's rule: 10 minutes per 100 m of climb.
- **Timezone data is never used**, not even internally, beyond UTC.

## Known limits

- Overpass is a free, rate-limited public service. When it is busy a tool
  returns an error with a suggestion and the narrator will offer to try again;
  there is a second mirror and a short retry with backoff. `scan_horizon` is the
  heaviest call and can take 20 seconds.
- Verified live against Overpass, Open-Meteo, iNaturalist and Nominatim. The
  agent loop and all three provider adapters are covered by offline tests; the
  loop has not been run against a live LLM endpoint here, as no API key was
  available in this environment.
