# Opsis on a Windows laptop

## 1. Docker Desktop

Install Docker Desktop with the **WSL 2 backend** (the default). Give WSL enough
memory: Opsis runs queries in memory, and Docker Desktop's default can be tight.
Create or edit `%UserProfile%\.wslconfig`:

```ini
[wsl2]
memory=8GB
processors=4
```

Then run `wsl --shutdown` in a terminal and start Docker Desktop again.

## 2. Opsis

```bash
git clone https://github.com/pundip/opsis.git
cd opsis
copy .env.local.example .env
docker compose up -d
```

Open http://localhost:8080. There is no sign-in on a laptop: you are the built-in
admin. Add `COMPOSE_PROFILES=sample` to `.env` for a sample MySQL database to try.

For development with hot reload (`npm run dev`), file watching is much faster when
the repository lives inside the WSL filesystem (for example `\\wsl$\Ubuntu\home\you\opsis`)
rather than on `C:\`.

## 3. Ollama (optional, for the in-app assistant of a later release)

Install Ollama for Windows from https://ollama.com and pull a model, for example
`ollama pull llama3.1`. Opsis reaches it at `http://host.docker.internal:11434`
(`OLLAMA_URL`). Running Ollama natively uses the GPU; the `ollama` Compose profile
runs it in a container instead, on the CPU.

## 4. Claude Code and Claude Desktop (MCP)

The repository's `.mcp.json` points Claude Code at `http://localhost:8080/mcp`, so
Claude Code started in the `opsis` folder finds the server by itself. From anywhere else:

```bash
claude mcp add --transport http opsis http://localhost:8080/mcp
```

Claude Desktop connects through the `mcp-remote` bridge. In
`%AppData%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "opsis": { "command": "npx", "args": ["-y", "mcp-remote", "http://localhost:8080/mcp"] }
  }
}
```

With `MCP_AUTH=token` (or for a server) add an access token, made under **Account**,
**Access tokens**: see [deploy/proxmox/README.md](../proxmox/README.md), section 6.
