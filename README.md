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

### Claude Code

Header support is built in:

```bash
claude mcp add --transport http tfnsw https://your-host/mcp --header "X-API-Key: YOUR_KEY"
```

### Claude Desktop

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
  returns **zero journeys** — verified against the live API. The old `"stop"`
  default silently failed every address-to-address trip.

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

- **Concise departures.** `get_departures` defaults to `concise=true`, returning
  only what a departure board shows:

  ```json
  {"departure_planned": "2026-09-29T17:24:00+10:00",
   "departure_estimated": "2026-09-29T17:25:06+10:00",
   "transportation": {"name": "Sydney Buses Network 333", "number": "333"}}
  ```

  `departure_estimated` is `null` when there is no live data. Pass
  `concise=false` for every field — the stop, platform, route details and
  onward locations. On a real Circular Quay board of 40 departures that is
  39.7 KB against 7.8 KB concise. The result reports `concise`, so a model can
  see it was trimmed.

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
  **v2** endpoint and the rest from v1; `tfnsw-trip-planner` 1.4.0 routes each
  feed to the version that actually serves it.

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

### Stdio (for local process-spawning clients and directory scanners)

```bash
MCP_TRANSPORT=stdio uv run python -m tfnsw_trip_planner_mcp
```

There are no HTTP headers under stdio, so every tool call fails with a missing-API-key
error — this mode only serves the `initialize`/`tools-list` handshake (e.g. for MCP
directory scanners that spawn the container and speak stdio rather than HTTP). The
default deployment above (Docker/Coolify, `/mcp` and `/sse`) is unaffected either way.

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
| `auth.py` | `X-API-Key` extraction and per-call client lifecycle |
| `serialization.py` | Library dataclasses → JSON-safe structures |
| `app.py` | ASGI app wiring both transports plus `/health` |

[![TfNSW Trip Planner MCP Server MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/maxim75/tfnsw_trip_planner_mcp/badges/card.svg)](https://glama.ai/mcp/servers/maxim75/tfnsw_trip_planner_mcp)
