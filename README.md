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
- **`get_vehicle_positions`** reads the GTFS-Realtime feed, which is a *separate*
  product on the Open Data portal — your key must be subscribed to it as well.
  Feeds can carry thousands of vehicles, so results are capped at `max_results`
  (default 100); `count` reports the true feed size and `returned` how many came
  back.

Results are returned as structured JSON: lists arrive as
`{"count": n, "<plural>": [...]}` and single lookups as `{"location": {...}}`.

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

## Layout

| File | Role |
|---|---|
| `server.py` | The 10 tools and their argument mapping |
| `auth.py` | `X-API-Key` extraction and per-call client lifecycle |
| `serialization.py` | Library dataclasses → JSON-safe structures |
| `app.py` | ASGI app wiring both transports plus `/health` |
