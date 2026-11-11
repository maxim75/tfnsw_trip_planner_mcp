"""Run the MCP server under uvicorn, or over stdio for local/CLI clients.

Examples::

    tfnsw-trip-planner-mcp                        # HTTP on $HOST:$PORT (default)
    tfnsw-trip-planner-mcp --transport stdio      # stdio, key from $TFNSW_API_KEY
    uvx --from git+https://github.com/maxim75/tfnsw_trip_planner_mcp \\
        tfnsw-trip-planner-mcp --transport stdio
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__

TRANSPORTS = ("http", "stdio")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    env_transport = os.environ.get("MCP_TRANSPORT", "").strip().lower() or "http"
    parser = argparse.ArgumentParser(
        prog="tfnsw-trip-planner-mcp",
        description="MCP server for the Transport for NSW Trip Planner APIs.",
    )
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default=env_transport if env_transport in TRANSPORTS else "http",
        help=(
            "http: Streamable HTTP at /mcp plus SSE at /sse; callers send their key in "
            "the X-API-Key header. stdio: one local client; the key comes from "
            "TFNSW_API_KEY. Defaults to $MCP_TRANSPORT, else http."
        ),
    )
    parser.add_argument("--host", help="HTTP bind address (default: $HOST or 0.0.0.0)")
    parser.add_argument("--port", type=int, help="HTTP port (default: $PORT or 6401)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    # Logging goes to stderr, which under stdio keeps it off the protocol stream.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger(__name__)

    if args.transport == "stdio":
        from .auth import API_KEY_ENV, set_stdio_api_key
        from .server import mcp

        if not set_stdio_api_key(os.environ.get(API_KEY_ENV)):
            # Still start: directory scanners spawn the server without a key and
            # only need the initialize/tools-list handshake to answer.
            log.warning("%s is not set; every tool call will fail until it is", API_KEY_ENV)
        log.info("Serving MCP over stdio")
        mcp.run(transport="stdio")
        return

    import uvicorn

    from .app import create_app, default_host, default_port

    host = args.host or default_host()
    port = args.port or default_port()
    log.info("Serving MCP on http://%s:%d/mcp (SSE at /sse)", host, port)
    uvicorn.run(create_app(host), host=host, port=port)


if __name__ == "__main__":
    main()
