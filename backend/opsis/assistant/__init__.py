"""The in-app assistant (brief 10.2): model providers, the tool loop and its budgets.

- `providers`: one model call to Claude (Anthropic) or to Ollama, streamed.
- `prompt`: the instructions, and the context sent with each request.
- `loop`: runs a request: model calls and tool calls until the model is done or
  a budget runs out. Tools are the MCP tools (`opsis.mcp.tools`), called directly.
- `runs`: requests in progress, so a person can stop one or follow it.
"""
