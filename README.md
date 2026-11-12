# TfNSW Trip Planner MCP Server

An [MCP](https://modelcontextprotocol.io) server exposing the
[Transport for NSW](https://opendata.transport.nsw.gov.au) trip planning APIs to
LLM clients, built on the
[`tfnsw-trip-planner`](https://github.com/maxim75/tfnsw_trip_planner) library.

Ten tools cover stop search, journey planning, live departure boards, service
alerts, nearby-stop lookup and live vehicle positions.

## Authentication

**The server stores no credentials.** Every caller supplies their own TfNSW Open
Data API key on every request:

```
X-API-Key: <your TfNSW API key>
```

Get a free key from the [TfNSW Open Data portal](https://opendata.transport.nsw.gov.au).
A request without the header gets an error naming the header rather than a
silent failure. `apikey <key>` and `Bearer <key>` forms are accepted too, since
TfNSW's own docs use the former.

Each tool call builds a client from that request's key and discards it when the
call returns, so one caller's key is never reused for another's request.

Over **stdio** (see [Running locally over stdio](#running-locally-over-stdio-uvx))
there are no headers: the server is spawned by a single local client, which
passes its key in the `TFNSW_API_KEY` environment variable instead. That variable
is read only in stdio mode — an HTTP deployment ignores it, so it can never end
up serving a key of its own to remote callers.

HTTP **connections** are nevertheless pooled process-wide, which cuts roughly
93ms — a TCP and TLS handshake — off every call. The split is deliberate: a
connection pool is keyed by host, not by credential, so it can be shared safely,
whereas the library writes the API key into `session.headers` and a shared
*session* would let one caller's key overwrite another's mid-flight.

## Endpoints

| Path | Purpose |
|---|---|
| `/mcp` | Streamable HTTP transport — use this |
| `/sse`, `/messages/` | Legacy SSE transport, for clients that need it |
| `/health` | Unauthenticated liveness probe |
| `/` | Service description |

Listens on `0.0.0.0:6401`; override with the `HOST` and `PORT` environment
variables.

## Connecting a client

### Running locally over stdio (uvx)

No hosted server needed: an MCP client can spawn the server itself with
[`uvx`](https://docs.astral.sh/uv/guides/tools/) and talk to it over stdio. The
key goes in `TFNSW_API_KEY`.

```bash
uvx --from git+https://github.com/maxim75/tfnsw_trip_planner_mcp tfnsw-trip-planner-mcp --transport stdio
```

Claude Code:

```bash
claude mcp add tfnsw --env TFNSW_API_KEY=YOUR_KEY -- uvx --from git+https://github.com/maxim75/tfnsw_trip_planner_mcp tfnsw-trip-planner-mcp --transport stdio
```

Claude Desktop (or any client with a JSON `mcpServers` config):

```json
{
  "mcpServers": {
    "tfnsw": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/maxim75/tfnsw_trip_planner_mcp",
        "tfnsw-trip-planner-mcp", "--transport", "stdio"
      ],
      "env": { "TFNSW_API_KEY": "YOUR_KEY" }
    }
  }
}
```

From a local checkout, use `--from /path/to/tfnsw_trip_planner_mcp` instead.
`MCP_TRANSPORT=stdio` works in place of `--transport stdio`. Without
`TFNSW_API_KEY` the server still starts and answers `initialize`/`tools/list`,
but every tool call returns an error naming the variable.

### Claude Code (hosted server)

Header support is built in:

```bash
claude mcp add --transport http tfnsw https://your-host/mcp --header "X-API-Key: YOUR_KEY"
```

### Claude Desktop (hosted server)

Claude Desktop's (and claude.ai's) native **"Add custom connector"** UI accepts a
URL and OAuth credentials only — it has **no field for a custom header**, so it
cannot be used with this server. Connect through the
[`mcp-remote`](https://github.com/geelen/mcp-remote) bridge instead (needs Node):

```json
{
  "mcpServers": {
    "tfnsw": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "https://your-host/mcp",
        "--transport", "http-only",
        "--header", "X-API-Key:${TFNSW_KEY}"
      ],
      "env": { "TFNSW_KEY": "YOUR_KEY" }
    }
  }
}
```

`mcp-remote` needs `Name:value` with **no space** after the colon. Config lives at
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, or
`%APPDATA%\Claude\claude_desktop_config.json` on Windows. Restart the app after
editing.

For a client that only speaks the older transport, point it at `/sse` and pass
`--transport sse-only`.

## Tools

`plan_trip` takes plain place names and resolves them itself. The other tools
are addressed by numeric stop ID, so resolve a name with `find_stop` or
`best_stop` first and pass the ID onwards.

| Tool | What it does |
|---|---|
| `find_stop` | Search stops, wharves, POIs and addresses by name |
| `find_stop_by_id` | Look up one stop by its numeric ID |
| `best_stop` | Return only the single best-matching location for a name |
| `plan_trip` | Plan a journey between two place names or stop IDs |
| `plan_trip_from_coordinate` | Plan a journey starting from a GPS coordinate |
| `plan_cycling_trip` | Plan a cycling route, optionally mixed with transit |
| `get_departures` | Live departure board for a stop or platform |
| `get_alerts` | Service alerts: disruptions, trackwork, planned changes |
| `find_nearby` | Stops and POIs near a coordinate, with distances |
| `get_vehicle_positions` | Live GPS positions of vehicles on a network |

Notes:

- **Times.** Tools taking a `when` accept ISO 8601, e.g. `2026-08-30T09:15`.
  Without an offset the value is Australia/Sydney local time. An unparseable
  value is rejected rather than ignored.
- **Errors.** A failed call is a tool error carrying TfNSW's own message. That
  includes the errors the trip planner reports inside an HTTP 200, such as an
  invalid origin or destination ID: those are errors now, not an empty result.
  "No route found" is still an empty list. `get_departures` is unchanged: TfNSW
  answers an unknown stop ID with an empty board, not an error. Network errors
  and HTTP 429/502/503/504 are retried twice with backoff before failing; a rate limit
  that outlasts the retries is reported as one, with the wait TfNSW asked for,
  so a model backs off instead of retrying into the same quota. (All of this
  needs `tfnsw-trip-planner` 1.5.0, the minimum this server depends on.)
- **Capped results.** `get_alerts`, `find_nearby` and `get_vehicle_positions`
  can each answer with far more than a caller can use — an unfiltered alert
  fetch returns every alert in NSW (~280, 1.3MB of JSON), and a 500m nearby
  search can return 600+ locations. They take a `max_results` (20, 50 and 100
  respectively), constrained to `>= 1`.

  This is not only about context budget: MCP sends each result twice (as text
  content and as structured content) and clients cap a single SSE event at
  1MiB, so an oversized reply fails outright with *"SSE stream ended without a
  response"*.
- **Place names.** `plan_trip` accepts `origin`/`destination` as free text
  ("100 Harris Street Pyrmont", "Bondi Junction") and resolves them server-side,
  so a model does not spend two `best_stop` round trips before planning can
  start. Pass `origin_id`/`destination_id` instead when you already hold an ID —
  giving both for the same end is an error rather than a silent preference. A
  resolved result echoes what each name matched:

  ```json
  {"origin": {"id": "...", "name": "100 Harris St, Pyrmont"}}
  ```

  Resolution refuses a weak match rather than guessing. TfNSW's stop finder
  practically never returns nothing — it returns its nearest guess with a score,
  and `"Zzzqqxnowhere Placeton"` really does resolve to *Iceton Pl, Yass*.
  Measured on the live API, real places score 250 (street addresses) to 996
  (stations) while nonsense scores 47–154, so anything under 200 is rejected
  with an error naming the candidate it declined to use.

- **`origin_type` defaults to `"any"`, not `"stop"`.** An address resolves to a
  `streetID:...` ID of type `singlehouse`, and planning it with `type_origin=stop`
  returned **zero journeys** — verified against the live API. The old `"stop"`
  default silently failed every address-to-address trip. (Since
  `tfnsw-trip-planner` 1.5.0 it fails loudly instead, with "stop invalid".)

- **Journey detail.** The journey tools take a `detail` level, because the raw
  response is overwhelmingly data a model never reads. Journey totals
  (`departure`, `arrival`, `duration_min`, `changes`, `via`) are always present,
  so the commonest questions need no leg data at all.

  | `detail` | includes | real Pyrmont → Engadine trip |
  |---|---|---|
  | `answer` | totals only | **1.4 KB** |
  | `summary` (default) | + legs: times, route, platform, alerts | 11.4 KB |
  | `stops` | + intermediate stop names | — |
  | `full` | + route polyline | — |

  Every result reports the level it used, so a model can see it was trimmed and
  ask for more rather than assuming the data does not exist. `full` only fits
  when paired with `max_results` of 1–3.

- **Concise results.** `get_departures`, `get_alerts` and `find_nearby` default
  to `concise=true`, returning only the fields a model reads. Pass
  `concise=false` for every field. Each result reports `concise`, so a model
  can see it was trimmed.

  | Tool | concise fields | real response, full → concise |
  |---|---|---|
  | `get_departures` | `departure_planned`, `departure_estimated`, `transportation.name`/`number` | Circular Quay, 40 departures: 39.7 → 7.8 KB |
  | `get_alerts` | `subtitle`, `url`, `last_modification`, `affected_lines`, `affected_stops` | Central (`stop_id=200060`): 56.8 → 2.4 KB |
  | `find_nearby` | `name`, `kind`, `distance`, `id` | Central, 500 m, 50 results: 18.9 → 2.8 KB |

  ```json
  {"departure_planned": "2026-09-29T17:24:00+10:00",
   "departure_estimated": "2026-09-29T17:25:06+10:00",
   "transportation": {"name": "Sydney Buses Network 333", "number": "333"}}
  ```

  `departure_estimated` is `null` when there is no live data.

  In concise alerts, `affected_lines` and `affected_stops` are deduplicated
  lists of line numbers and stop names. Full, each affected line is a whole
  object, and trip-level alerts embed the trip's timetable. One Central alert
  lists 163 lines, which made a single-stop fetch *larger* than a network-wide
  one.

  In concise nearby results, `kind` is the TfNSW draw class (`StopPoint`,
  `StopArea`, `EntryExit`, `Bench`, …), because the raw `type` is `unknown` on
  every result and most results have no name. `id` appears only on stops, taken
  from the stop ID TfNSW puts in `properties.description`. Every raw `id` is a
  `coord:...` string that no other tool accepts.

- **Legs are built from an allowlist**, not by dropping known-bad fields. The
  old blocklist removed `coords` and `stop_sequence` and passed everything else
  through, including the raw upstream `properties` bag — lift equipment heights,
  `AREA_NIVEAU_DIVA`, `areaGid`, `pbyb`, and the platform name repeated under
  three separate keys. On a real trip that passthrough was 28% of the payload.
  An allowlist cannot regress that way when TfNSW adds a field. Also:

  - the four planned/estimated time fields collapse to the time that will
    actually happen, plus `scheduled_*` **only** when there is a real delay
    (20 of 22 timed stops on a real trip had the two exactly equal);
  - null times, empty alert lists and the empty `transportation` block that
    every walking leg carries are omitted rather than serialized;
  - stop IDs are echoed for chaining only when they are real stop IDs, not
    129-byte composite `streetID:...` blobs that no other tool accepts.

  Together with the resolution above, the question *"when do I arrive at 32
  Geelong Rd if I leave now from 100 Harris Street Pyrmont"* went from **3 calls
  and ~10,800 tokens to 1 call and ~356 tokens** (`detail="answer"`).

- **`find_stop` takes `limit`, not `max_results`** — deliberately a different
  name, because it bounds the *upstream query* rather than truncating a fetched
  list, so `count` is exact and no bandwidth is wasted.
- **`get_vehicle_positions`** reads the GTFS-Realtime feed, which is a *separate*
  product on the Open Data portal — your key must be subscribed to it as well.
  An unrecognised `mode` is rejected with the list of valid feeds rather than
  being passed through to an opaque upstream 404. The nine valid feeds are
  `buses`, `sydneytrains`, `metro`, `nswtrains`, `ferries/sydneyferries`,
  `lightrail/cbdandsoutheast`, `lightrail/innerwest`, `lightrail/newcastle` and
  `lightrail/parramatta`.

  The list is taken from the library rather than restated here, so the two
  cannot drift. TfNSW serves `sydneytrains` and `lightrail/innerwest` from a
  **v2** endpoint and the rest from v1; `tfnsw-trip-planner` routes each
  feed to the version that actually serves it (since 1.4.0).

Results are returned as structured JSON. Every list-returning tool answers with
the same shape, so `returned` is always present and `count` is always the true
total before any capping:

```json
{"count": 618, "returned": 50, "locations": [...]}
```

Single lookups (`find_stop_by_id`, `best_stop`) return `{"location": {...}}`,
or `{"location": null}` when nothing matches.

## Running it

### Docker (how it is deployed)

```bash
docker compose up --build -d
```

```bash
curl -sf localhost:6401/health
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for the Coolify setup.

### Local development

```bash
uv sync
```

```bash
uv run python -m tfnsw_trip_planner_mcp
```

`--host` and `--port` override `HOST` and `PORT`; `--help` lists every option.

### Stdio

```bash
TFNSW_API_KEY=your_key uv run tfnsw-trip-planner-mcp --transport stdio
```

See [Running locally over stdio](#running-locally-over-stdio-uvx) for client
configs. Directory scanners that spawn the container speak stdio too; they need
no key, since they only check the `initialize`/`tools/list` handshake. Run the
image that way with `-e MCP_TRANSPORT=stdio`. The default deployment above
(Docker/Coolify, `/mcp` and `/sse`) is unaffected.

### Tests

The default suite is fully offline — the library client is mocked, so no key is
needed and no request leaves the machine:

```bash
uv run pytest
```

Smoke tests against the real API are opt-in and skipped unless a key is present:

```bash
TFNSW_API_KEY=your_key uv run pytest -m live
```

### CI

GitHub Actions runs on every push and pull request: ruff, the offline suite,
and a Docker job that builds the image, waits for `/health`, and checks the
running container lists all 10 tools.

The live tests run on `main` and on manual dispatch. They **skip themselves**
unless a `TFNSW_API_KEY` repository secret exists, so CI is green without one —
add it under *Settings → Secrets and variables → Actions* to enable them. Fork
pull requests never receive the secret, so they always skip.

## Layout

| File | Role |
|---|---|
| `server.py` | The 10 tools and their argument mapping |
| `auth.py` | `X-API-Key` extraction (or `TFNSW_API_KEY` under stdio) and per-call client lifecycle |
| `serialization.py` | Library dataclasses → JSON-safe structures |
| `app.py` | ASGI app wiring both transports plus `/health` |
| `__main__.py` | CLI entry point: `--transport http` (default) or `stdio` |

[![TfNSW Trip Planner MCP Server MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/maxim75/tfnsw_trip_planner_mcp/badges/card.svg)](https://glama.ai/mcp/servers/maxim75/tfnsw_trip_planner_mcp)
