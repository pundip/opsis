"""The MCP server: tools, resources and a prompt over the same services as REST.

`tools.py` holds the tool definitions and their implementations as plain
functions, so the in-app assistant (M7) can call them without MCP in between.
`server.py` exposes them over Streamable HTTP at {BASE_PATH}/mcp.
"""
