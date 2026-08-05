"""MCP server that wraps the Underlying Analyzer public HTTP API."""

__all__ = ["main"]


def main() -> None:
    from mcp_server.server import main as run_server

    run_server()
