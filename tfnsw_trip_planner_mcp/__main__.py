"""Run the MCP server under uvicorn."""

from __future__ import annotations

import logging

import uvicorn

from .app import create_app, default_host, default_port


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host, port = default_host(), default_port()
    logging.getLogger(__name__).info("Serving MCP on http://%s:%d/mcp (SSE at /sse)", host, port)
    uvicorn.run(create_app(host), host=host, port=port)


if __name__ == "__main__":
    main()
