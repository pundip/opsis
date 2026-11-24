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
| `ANTHROPIC_API_KEY` | for the editor's assistant (Claude), from the Claude Console. Or set `FEATURE_ASSISTANT=false`, or `ASSISTANT_PROVIDER=ollama` with an Ollama the LXC can reach (`OLLAMA_URL`) |
| `FEATURE_EMAIL` and `SMTP_*` | optional: a mail server for invitations, resets and emailed dashboards (section 8) |
| `BACKUP_SCHEDULE`, `BACKUP_RETENTION` | when backups run and how many are kept; the defaults are 02:30 and 7 (section 7) |

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

**Checklist: Google sign-in on the real server.** Run it once after the first start, and
again after any change to the proxy or the OAuth client. It is part of the install check.

1. In Google Cloud, the OAuth consent screen is set up (*Internal* if your accounts are
   in one Google Workspace; otherwise *External*, with your test accounts added while it
   is in testing), and the client's redirect URI and JavaScript origin are exactly as
   above: `https`, no trailing slash on the origin, `/opsis` in the redirect URI.
2. `.env` has `AUTH_MODE=full`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and
   `GOOGLE_ALLOWED_DOMAINS` (or `GOOGLE_ALLOWED_EMAILS`); `docker compose up -d` after a change.
3. The LXC can reach Google: `docker compose exec api python -c "import urllib.request; print(urllib.request.urlopen('https://accounts.google.com/.well-known/openid-configuration').status)"`
   prints `200`.
4. As an admin, invite an allowed address (Users, Invite) as a viewer.
5. In a private window, open `https://urlloom.com/opsis/`, choose **Sign in with Google**
   and pick that account. You land in Opsis signed in, and **Account** shows "Google: Linked".
6. Refusals: a Google account outside the allowed domains gets "not allowed to sign in";
   an allowed address that was not invited gets "has not been invited".
7. In the browser's developer tools, the `opsis_session` cookie has `Path=/opsis`,
   `Secure` and `HttpOnly`.
8. **Audit log** shows `auth.login` with method `google` for step 5, and `auth.login_failed`
   with the reasons for step 6, each from your own address (not the proxy's: see above).

If Google reports `redirect_uri_mismatch`, the URI in Google Cloud differs from
`PUBLIC_BASE_URL` + `/api/auth/google/callback`. If the sign-in page says it took too
long or was started in another browser, the proxy is dropping cookies or the server's
clock is far off. If it says Google could not be reached, the LXC has no outbound
HTTPS (step 3).

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

With `FEATURE_BACKUPS=true` (the server default) the worker makes a backup every night
at 02:30 (`BACKUP_SCHEDULE`, a cron expression in `WORKSPACE_TIMEZONE`) into the
`backups` volume, which is `/backups` inside the api and worker containers. A backup is
one file, `opsis-<date>-<time>.tar`, holding:

- the metadata database: people, groups, connections, models, dashboards and every
  version, schedules and the audit log;
- the uploaded CSV and Excel files;
- each dataset's current data, so dashboards work straight after a restore, before any
  source is reached.

The newest `BACKUP_RETENTION` files (7) are kept. **Settings** lists them and has
**Back up now**; from the LXC's shell: `docker compose exec api python -m opsis backup`.

**Not in a backup: `.env`.** Keep a copy of it away from the server (it holds
secrets, so somewhere private). Its `ENCRYPTION_KEY` is needed to read the connection
passwords and keys saved in a restored database.

### Backups on NFS

To keep backups off the LXC, give the container an NFS directory and point the
`backups` volume at it, **before the first start** (or after copying the existing
backups out and running `docker volume rm opsis_backups`).

1. On the Proxmox host, mount the NFS export (for example in `/etc/fstab`) at
   `/mnt/nas/opsis-backups`, and pass it into the container:

   ```bash
   pct set 120 -mp0 /mnt/nas/opsis-backups,mp=/mnt/backups
   ```

2. The Opsis containers write as uid 10001. In an unprivileged LXC that is uid
   **110001** on the Proxmox host, so the export must let that uid write: either
   `chown 110001:110001` the directory on the NAS, or export it with
   `all_squash,anonuid=110001,anongid=110001`.

3. In `/opt/opsis`, create `compose.override.yml` (Compose reads it with `compose.yml`
   automatically):

   ```yaml
   volumes:
     backups:
       driver: local
       driver_opts:
         type: none
         o: bind
         device: /mnt/backups
   ```

4. `docker compose up -d`, then **Back up now** in Settings: the file appears on the NAS.

For the whole container (Docker, the OS and `.env` included), also schedule a Proxmox
backup of the LXC to your NAS storage (Datacenter, Backup, or `vzdump 120 --mode snapshot`).

### Restore

A restore replaces the database, the uploads and the datasets with the backup's. The
previous uploads and datasets are moved aside (to `/data/before-restore-<time>` in the
`opsisdata` volume), not deleted, so a mistaken restore can be undone by hand.

```bash
cd /opt/opsis
docker compose stop api worker
docker compose run --rm api restore opsis-20261124-023000.tar --yes
docker compose up -d
```

The file name is looked up in `/backups`. Without `--yes` the command shows what the
backup holds (date and version) and changes nothing. The api upgrades a restored
database to the current version when it starts.

**Onto a new server:** install as in sections 1 to 3 with the **same `ENCRYPTION_KEY`**
(the other values may differ), start it once, copy the backup in and restore:

```bash
docker compose up -d
docker compose cp opsis-20261124-023000.tar api:/backups/
docker compose stop api worker
docker compose run --rm api restore opsis-20261124-023000.tar --yes
docker compose up -d
```

## 8. Email

Email is off by default (`FEATURE_EMAIL=false`); admins then pass invitation and reset
links on themselves. With a mail server, set in `.env`:

```ini
FEATURE_EMAIL=true
SMTP_HOST=smtp.example.org
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=bi@example.org
SMTP_PASSWORD=...
SMTP_FROM=Opsis <bi@example.org>
```

then `docker compose up -d`, and in **Settings** choose **Send me a test email**.
`SMTP_SECURITY` is `starttls` (usually port 587), `ssl` (port 465) or `none` (a relay on
your own network). Leave `SMTP_USERNAME` and `SMTP_PASSWORD` empty for a relay that
needs no sign-in. Some common services: Google Workspace's SMTP relay
(`smtp-relay.gmail.com`, port 587, set up in the Admin console), Gmail with an app
password (`smtp.gmail.com`, 587), Microsoft 365 with SMTP AUTH enabled for the mailbox
(`smtp.office365.com`, 587). The LXC needs outbound access to that port.

With email on, Opsis emails invitations and password reset links, the sign-in page
offers **Forgotten your password?**, and people can have a dashboard emailed to them on
a schedule (**Email me** on a dashboard). Links in emails start with `PUBLIC_BASE_URL`.
Messages are never stored. A failed send is in the api's or the worker's log
(`docker compose logs worker | grep -i email`); for an invitation the admin also gets
the link on screen, with the reason.

## 9. Upgrades

```bash
cd /opt/opsis && git pull && docker compose up -d --build
```

Database migrations run when the api starts. Make a backup first (**Back up now**).

## 10. Locked out

If no admin can sign in, run on the LXC:

```bash
docker compose exec api python -m opsis reset-link admin@example.org
```

It prints a one-time link (valid 24 hours) to set that person's password, and
reactivates the account. Anyone who can run this already controls the server.

## 11. Install check

Run this once on a new install, from this guide and the README only. It is the final
check of a release.

1. **Fresh install:** sections 1 to 5, with your own values in `.env`.
2. **Healthy:** `docker compose logs config-check` ends with "Configuration is valid";
   `docker compose ps` shows `postgres`, `api` and `worker` running, `api` healthy.
3. **First sign-in:** open `https://urlloom.com/opsis/` and sign in with `ADMIN_EMAIL`
   and `ADMIN_INITIAL_PASSWORD`; you are asked to choose a new password.
4. **Real client addresses:** **Audit log** shows your sign-in from your own computer's
   address, not the proxy's (section 3, `TRUSTED_PROXIES`).
5. **Google sign-in:** the checklist in section 3.
6. **Demo data:** **Settings**, **Load demo data**, then **Open the demo dashboard**:
   eight visuals with numbers, and the date and fund filters change them.
7. **Backups:** **Settings**, **Back up now**: a new file in the list (and on the NAS,
   with section 7's NFS set-up). If you can spare a test LXC, restore it there.
8. **Email** (if on): **Send me a test email** arrives; an invitation to a second address
   arrives and its link lets that person set a password.
9. **Firewall:** from a computer other than the proxy, `curl -m 5 http://192.168.1.50:8080/opsis/healthz`
   fails (section 5), while the site works through the proxy.
10. **MCP:** with an access token (section 6), Claude Code lists your dashboards.
11. **A real source:** a connection to one of your own sources tests and saves, and a
    dataset from it refreshes.

## 12. Troubleshooting

| Symptom | Likely cause |
|---|---|
| The stack does not start | `docker compose logs config-check` names the setting and how to fix it |
| 404 for everything under `/opsis` | the proxy strips the prefix (`proxy_pass .../;` with a path, or Caddy `handle_path`) |
| Signed out after every click | the proxy rewrites `Host` or the path; cookies are scoped to `/opsis` |
| The editor does not update live, or MCP hangs | the proxy buffers: `proxy_buffering off` / `flush_interval -1` |
| Uploads fail with 413 | the proxy's body limit is lower than `MAX_UPLOAD_MB` |
| Google says `redirect_uri_mismatch` | the redirect URI in Google Cloud differs from `PUBLIC_BASE_URL` + `/api/auth/google/callback` |
| Every sign-in in the audit log shows the proxy's address | `TRUSTED_PROXIES` does not list the address the api sees (section 3) |
| A backup fails with "permission denied" on NFS | the export does not let uid 110001 write (section 7) |
| The test email fails | the message names the problem: a wrong `SMTP_HOST` or port, the LXC cannot reach it, or the mail server refused the sign-in |
| Scheduled refreshes or backups do not run | the `worker` container is not running (`docker compose ps`); it runs every job and schedule in server mode |
