"""Run the MCP server under uvicorn, or over stdio for local/CLI clients."""

from __future__ import annotations

import logging
import os

import uvicorn

from .app import create_app, default_host, default_port
from .server import mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if os.environ.get("MCP_TRANSPORT", "").lower() == "stdio":
        # No HTTP headers exist under stdio, so every tool call fails with
        # MissingAPIKeyError (see auth.client_for) — this mode only serves
        # clients that spawn the process locally and set their own API key
        # some other way, or directory scanners that just need the handshake
        # (initialize/tools-list) to answer.
        logging.getLogger(__name__).info("Serving MCP over stdio")
        mcp.run(transport="stdio")
        return

    host, port = default_host(), default_port()
    logging.getLogger(__name__).info("Serving MCP on http://%s:%d/mcp (SSE at /sse)", host, port)
    uvicorn.run(create_app(host), host=host, port=port)


if __name__ == "__main__":
    main()
