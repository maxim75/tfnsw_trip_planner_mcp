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

Stops are addressed by numeric ID, so resolve a name with `find_stop` or
`best_stop` first, then pass the ID onwards.

| Tool | What it does |
|---|---|
| `find_stop` | Search stops, wharves, POIs and addresses by name |
| `find_stop_by_id` | Look up one stop by its numeric ID |
| `best_stop` | Return only the single best-matching location for a name |
| `plan_trip` | Plan a journey between two stop IDs |
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
- **`find_stop` takes `limit`, not `max_results`** — deliberately a different
  name, because it bounds the *upstream query* rather than truncating a fetched
  list, so `count` is exact and no bandwidth is wasted.
- **`get_vehicle_positions`** reads the GTFS-Realtime feed, which is a *separate*
  product on the Open Data portal — your key must be subscribed to it as well.
  An unrecognised `mode` is rejected with the list of valid feeds rather than
  being passed through to an opaque upstream 404.

Results are returned as structured JSON. Every list-returning tool answers with
the same shape, so `returned` is always present and `count` is always the true
total before any capping:

```json
{"count": 618, "returned": 50, "locations": [...]}
```

Single lookups (`find_stop_by_id`, `best_stop`) return `{"location": {...}}`,
or `{"location": null}` when nothing matches.

### Known limitation: empty location names

`find_stop`, `find_stop_by_id`, `best_stop` and `find_nearby` return correct
stop **IDs** but an empty `name`. This is an upstream bug in
`tfnsw-trip-planner` 1.3.1: `Location.from_dict` reads the name only from
`properties.STOP_NAME_WITH_PLACE`, which the coordinate API supplies but the
stop-finder API does not — the latter returns it at the top level as
`data["name"]` (`"Circular Quay, Sydney"`). The name is discarded during
parsing, so this server cannot recover it downstream; the fix belongs in the
library, mirroring the fallback its `id` field already has.

`get_departures` is unaffected — it uses a different model that parses names
correctly. `tests/test_live.py` carries an `xfail` test that will flip to
passing once the library is fixed.

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
