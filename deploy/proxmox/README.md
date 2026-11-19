# Opsis on Proxmox (LXC with Docker)

This sets Opsis up in an unprivileged Ubuntu container (LXC) on Proxmox, behind a
reverse proxy you already run, at a sub-path such as `https://urlloom.com/opsis`.
The proxy handles TLS; Opsis listens on plain HTTP on the LXC's LAN address.

```
browser ──https──> your reverse proxy ──http──> LXC_IP:8080 (Opsis api)
                   (urlloom.com/opsis/...)       postgres, worker: inside the LXC only
```

## 1. Create the container

Suggested size: 4 vCPU, 8 GB RAM, 2 GB swap, and a disk of 40 GB plus about three
times the size of the data you load (Opsis keeps a few Parquet snapshots per dataset).

From the Proxmox shell (adjust the ID, storage, bridge and addresses):

```bash
pveam update && pveam download local ubuntu-24.04-standard_24.04-2_amd64.tar.zst
pct create 120 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname opsis --cores 4 --memory 8192 --swap 2048 \
  --rootfs local-lvm:60 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.1.50/24,gw=192.168.1.1 \
  --unprivileged 1 --features nesting=1,keyctl=1 --onboot 1
pct start 120 && pct enter 120
```

`nesting=1` and `keyctl=1` are what Docker needs inside an unprivileged container.

## 2. Install Docker Engine (inside the LXC)

```bash
apt-get update && apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
docker run --rm hello-world
```

## 3. Configure Opsis

```bash
git clone https://github.com/pundip/opsis.git /opt/opsis && cd /opt/opsis
cp .env.server.example .env
docker compose run --rm --no-deps api generate-key   # paste the output into ENCRYPTION_KEY
```

Edit `.env` and replace every value marked `CHANGE`:

| Setting | Value |
|---|---|
| `PUBLIC_BASE_URL` | `https://urlloom.com/opsis` (the address people type; its path becomes the base path) |
| `TRUSTED_PROXIES` | the address the api sees your proxy connect from (see the note below) |
| `BIND_ADDRESS` | the LXC's LAN IP, for example `192.168.1.50` (or `0.0.0.0`) |
| `POSTGRES_PASSWORD` | a long random value, before the first start |
| `ENCRYPTION_KEY` | from `generate-key`. **Keep a copy outside the server**: without it saved connection passwords cannot be read |
| `ADMIN_EMAIL`, `ADMIN_INITIAL_PASSWORD` | the first admin; you choose a new password at the first sign-in |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | from Google Cloud, see below |
| `GOOGLE_ALLOWED_DOMAINS` / `GOOGLE_ALLOWED_EMAILS` | who may sign in with Google |

**Which address goes in `TRUSTED_PROXIES`.** Opsis only believes `X-Forwarded-For`
and `X-Forwarded-Proto` from these addresses. It must be the address the api sees,
which is not always the proxy's own: with port forwarding or NAT in front of the
LXC it can be the forwarding host or the Docker gateway. To check, sign in once and
open **Audit log**: the address shown next to your sign-in should be your own
computer's. If it shows the proxy's or the gateway's address, add that address to
`TRUSTED_PROXIES` and restart.

**Google sign-in.** In Google Cloud Console, under APIs and services, Credentials,
create an OAuth client ID of type *Web application* with:

- Authorised JavaScript origin: `https://urlloom.com`
- Authorised redirect URI: `https://urlloom.com/opsis/api/auth/google/callback`

Only people you invite can sign in with Google (`GOOGLE_AUTO_PROVISION=false`), and
only if their address passes the allowlists. Google must report the email as verified.

Start it:

```bash
docker compose up -d
docker compose logs config-check   # explains any problem with .env, then exits
docker compose ps                  # api and worker should become healthy
```

A bad `.env` stops the stack once with the reason in `config-check`'s log; fix the
value and run `docker compose up -d` again.

## 4. Your reverse proxy

The proxy must forward the path **unchanged** (Opsis owns `/opsis`), must not buffer
responses (live editor updates and MCP stream), and should allow uploads as large as
`MAX_UPLOAD_MB`.

### Nginx

```nginx
location = /opsis {
    return 301 /opsis/;
}

location /opsis/ {
    # No path after the port: the /opsis prefix is forwarded unchanged.
    proxy_pass http://192.168.1.50:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";

    # Server-sent events (live editor updates) and MCP: no buffering, long reads.
    proxy_buffering off;
    proxy_read_timeout 3600s;

    # Match MAX_UPLOAD_MB in .env.
    client_max_body_size 200m;
}
```

This is the same block the `proxytest` profile uses on a laptop
(`deploy/proxytest/nginx.conf`), where it is tested by `npm run smoke`.

### Caddy (an existing Caddy in front of several apps)

```caddyfile
urlloom.com {
    redir /opsis /opsis/ 308

    # handle keeps the /opsis prefix (handle_path would strip it, which breaks Opsis).
    handle /opsis/* {
        reverse_proxy 192.168.1.50:8080 {
            flush_interval -1
        }
    }

    # ... your other apps ...
}
```

Caddy sets `X-Forwarded-For` and `X-Forwarded-Proto` itself. Raise its request body
limit only if you have lowered it elsewhere.

### No reverse proxy yet: the bundled Caddy

For an install with nothing in front of it, add `caddy` to `COMPOSE_PROFILES` and set
`SITE_ADDRESS=bi.example.com` (a domain that points at this machine, ports 80 and 443
reachable): Caddy then gets certificates from Let's Encrypt. Set
`TRUSTED_PROXIES=172.16.0.0/12` so Opsis trusts Caddy on the Docker network, and
`PUBLIC_BASE_URL=https://bi.example.com`.

### Cloudflare Tunnel

Add `tunnel` to `COMPOSE_PROFILES`, set `CLOUDFLARE_TUNNEL_TOKEN`, and point the
tunnel's public hostname at `http://api:8080` (or at `http://caddy:80` together with
the `caddy` profile and `SITE_ADDRESS=:80`). Set `TRUSTED_PROXIES=172.16.0.0/12`.

## 5. Only the proxy may reach port 8080

Anything that can reach `LXC_IP:8080` directly could send its own `X-Forwarded-*`
headers if it were trusted, and bypasses the proxy's TLS. Allow only the proxy.

Docker publishes ports through its own iptables rules, which **bypass ufw inside the
container**, so use the Proxmox firewall (recommended) or Docker's `DOCKER-USER` chain:

- **Proxmox firewall:** on the container, Firewall, Options: enable it with *Input
  policy* `DROP`; add a rule *IN, ACCEPT, source = proxy IP, dest. port 8080, TCP*;
  add rules for anything else you need (SSH from your admin network). Enable the
  firewall on the container's network device too (`firewall=1` on `net0`).
- **Inside the LXC:** `iptables -I DOCKER-USER -p tcp --dport 8080 ! -s 192.168.1.10 -j DROP`
  (the proxy's address), saved with `apt-get install iptables-persistent`.

## 6. AI clients (MCP)

With `MCP_AUTH=token` every client needs a personal access token: sign in, open
**Account**, then **Access tokens**, and create one (read, edit and, if wanted, publish).

```bash
claude mcp add --transport http opsis https://urlloom.com/opsis/mcp --header "Authorization: Bearer opsis_pat_..."
```

Claude Desktop, through `mcp-remote` (in `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "opsis": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://urlloom.com/opsis/mcp", "--header", "Authorization:${OPSIS_AUTH}"],
      "env": { "OPSIS_AUTH": "Bearer opsis_pat_..." }
    }
  }
}
```

The same tokens work for scripts on the REST API, for example
`curl -H "Authorization: Bearer opsis_pat_..." https://urlloom.com/opsis/api/dashboards`.

## 7. Backups and restore

What matters: the Postgres database (users, connections, models, dashboards and
versions, audit log), the `opsisdata` volume (Parquet snapshots and uploaded files),
and `.env` (above all `ENCRYPTION_KEY`).

The simplest complete backup is a Proxmox backup of the whole container
(Datacenter, Backup, or `vzdump 120 --mode snapshot`). For finer-grained copies:

```bash
cd /opt/opsis
docker compose exec -T postgres pg_dump -U opsis -Fc opsis > /backups/opsis-$(date +%F).dump
docker run --rm -v opsis_opsisdata:/data:ro -v /backups:/backup alpine \
  tar czf /backup/opsisdata-$(date +%F).tgz -C /data .
cp .env /backups/opsis-env-$(date +%F)   # contains secrets: keep the backups private
```

Restore onto a fresh install with the same `.env`:

```bash
docker compose up -d postgres
docker compose exec -T postgres pg_restore -U opsis -d opsis --clean --if-exists < opsis-2026-11-19.dump
docker run --rm -v opsis_opsisdata:/data -v /backups:/backup alpine \
  sh -c "rm -rf /data/* && tar xzf /backup/opsisdata-2026-11-19.tgz -C /data"
docker compose up -d
```

A scheduled backup job inside Opsis (`FEATURE_BACKUPS`) arrives in a later release.

## 8. Upgrades

```bash
cd /opt/opsis && git pull && docker compose up -d --build
```

Database migrations run when the api starts. Take a backup first.

## 9. Locked out

If no admin can sign in, run on the LXC:

```bash
docker compose exec api python -m opsis reset-link admin@example.org
```

It prints a one-time link (valid 24 hours) to set that person's password, and
reactivates the account. Anyone who can run this already controls the server.

## 10. Troubleshooting

| Symptom | Likely cause |
|---|---|
| The stack does not start | `docker compose logs config-check` names the setting and how to fix it |
| 404 for everything under `/opsis` | the proxy strips the prefix (`proxy_pass .../;` with a path, or Caddy `handle_path`) |
| Signed out after every click | the proxy rewrites `Host` or the path; cookies are scoped to `/opsis` |
| The editor does not update live, or MCP hangs | the proxy buffers: `proxy_buffering off` / `flush_interval -1` |
| Uploads fail with 413 | the proxy's body limit is lower than `MAX_UPLOAD_MB` |
| Google says `redirect_uri_mismatch` | the redirect URI in Google Cloud differs from `PUBLIC_BASE_URL` + `/api/auth/google/callback` |
| Every sign-in in the audit log shows the proxy's address | `TRUSTED_PROXIES` does not list the address the api sees (section 3) |
