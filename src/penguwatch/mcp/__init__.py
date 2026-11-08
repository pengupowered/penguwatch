"""The read-only MCP endpoint (POST /mcp): a small server of the Model Context Protocol, written here so the app needs
no SDK at run time (the `mcp` package is a test dependency only: the reference client the tests talk to it with).
protocol.py is the JSON-RPC and MCP rules, tools.py the tools, route.py the FastAPI route. docs/mcp.md."""
