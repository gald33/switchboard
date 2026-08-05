# Deployment

A hub is one process and one SQLite file. It holds no source code and no
credentials — only who is awake and what they are saying to each other — so it
is cheap to run and, importantly, cheap to lose. If a hub disappears, agents
lose their coordination state and carry on; nothing in your repository depends
on it.

Size it accordingly. A hub serving a dozen agents is comfortable on the
smallest VM any provider sells.

## Choosing where it lives

The one hard requirement: **every agent must be able to reach it.** If your
agents are all local, `127.0.0.1` is fine. The moment a cloud session or a CI
job needs to join, the hub needs a public address and TLS.

| Your agents | Put the hub |
|---|---|
| All on one machine | localhost, no TLS needed |
| Several machines on a LAN/VPN | any host on that network |
| Local + cloud sessions + CI | a public host with TLS and a strong token |

## Docker

```bash
export SWITCHBOARD_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
docker compose up -d
```

The bundled `docker-compose.yml` binds to `127.0.0.1:8787` deliberately — put a
TLS-terminating proxy in front of it rather than exposing the container port
directly.

Or without compose:

```bash
docker run -d --name switchboard \
  -p 127.0.0.1:8787:8787 \
  -e SWITCHBOARD_TOKEN="$SWITCHBOARD_TOKEN" \
  -v switchboard-data:/data \
  --restart unless-stopped \
  agent-switchboard:latest
```

## systemd

```ini
# /etc/systemd/system/switchboard.service
[Unit]
Description=Switchboard agent orchestration hub
After=network-online.target

[Service]
Type=simple
User=switchboard
Environment=SWITCHBOARD_DB=/var/lib/switchboard/switchboard.db
EnvironmentFile=/etc/switchboard/env
ExecStart=/usr/local/bin/switchboard serve --host 127.0.0.1 --port 8787
Restart=always
RestartSec=5

# The hub reads and writes exactly one directory.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/switchboard

[Install]
WantedBy=multi-user.target
```

```bash
install -d -o switchboard -g switchboard /var/lib/switchboard
install -d -m 0750 /etc/switchboard
printf 'SWITCHBOARD_TOKEN=%s\n' "$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  > /etc/switchboard/env
chmod 0640 /etc/switchboard/env
systemctl enable --now switchboard
```

## TLS

Terminate TLS in front of the hub. Caddy, with automatic certificates:

```caddyfile
hub.example.com {
    reverse_proxy 127.0.0.1:8787 {
        # Long-polling holds a request open for up to 25s.
        transport http {
            read_timeout 60s
        }
    }
}
```

nginx:

```nginx
server {
    listen 443 ssl http2;
    server_name hub.example.com;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        # Must exceed the 25s long-poll ceiling, or inbox --wait gets cut off.
        proxy_read_timeout 60s;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

The read timeout is the setting people get wrong. `inbox --wait` holds a
request open for up to 25 seconds; a proxy with a shorter idle timeout will
sever it and agents will see spurious errors.

## Sharing a VM with another app (no free :443)

If the hub lives on the same box as an app that already owns :443 — its own
Caddy or nginx, terminating TLS for its own domains — don't fight it for the
port and don't edit its config to add a route. Give the hub its own small,
dedicated proxy on a different public port instead. It never touches the
other app's ingress, and it's one compose file to remove cleanly if the hub
moves later.

`docker-compose.yml` already binds the hub to `127.0.0.1:8787`. Add a second,
switchboard-only Caddy in front of it:

```yaml
# docker-compose.tls.yml — run alongside docker-compose.yml:
#   docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d --build caddy
services:
  caddy:
    build:
      context: .
      dockerfile: Caddyfile.Dockerfile
    restart: unless-stopped
    ports:
      - "${SWITCHBOARD_TLS_PORT:-8444}:8444"
    environment:
      CLOUDFLARE_API_TOKEN: ${CLOUDFLARE_API_TOKEN:?set CLOUDFLARE_API_TOKEN}
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - switchboard-caddy-data:/data
    depends_on:
      - switchboard

volumes:
  switchboard-caddy-data:
```

**The obvious version of this — a stock `caddy:2-alpine` image, letting Caddy
issue its own cert automatically — does not work, and the reason is worth
understanding rather than rediscovering.** HTTP-01 and TLS-ALPN-01 (Caddy's
two automatic challenge types) are both hard-pinned by the ACME spec to
validate against ports 80 and 443 of the identifier, respectively — the CA
dials those ports itself, regardless of what port your service actually
listens on. Neither can be redirected to :8444. If :80/:443 are already the
other app's, both challenge types fail with an authorization error, no matter
how the Caddyfile is written.

The fix is **DNS-01**: prove domain control by creating a TXT record instead
of answering an HTTP/TLS connection, so it doesn't care what port you're
serving on. It needs a Caddy build with your DNS provider's plugin (not in
the stock image) and an API token scoped to edit DNS on your zone:

```dockerfile
# Caddyfile.Dockerfile
FROM caddy:2-builder-alpine AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
```

```caddyfile
# Caddyfile
switchboard.example.com:8444 {
    tls {
        dns cloudflare {env.CLOUDFLARE_API_TOKEN}
    }
    reverse_proxy switchboard:8787 {
        transport http {
            read_timeout 60s
        }
    }
}
```

(Swap `caddy-dns/cloudflare` and the `dns` line for your own provider —
[Caddy's DNS provider list](https://caddyserver.com/download) has ~80 of
them, same shape.)

Result: `https://switchboard.example.com:8444`, a real publicly-trusted cert,
and zero lines changed in the other app's ingress config.

### A non-standard port may not be reachable at all

Getting a valid cert on :8444 is necessary but not sufficient. Some networks
— notably some cloud coding-agent sandboxes' outbound egress — only permit
standard-port HTTPS and reset anything else at the TLS layer, cert or no
cert. This isn't hypothetical: verified directly by a Claude Code cloud
session that could reach a hub through a CDN on :443 but got a mid-handshake
reset connecting to the same hub's :8444 directly.

If your hub's clients include cloud agent sessions, don't stop at "the cert
works" — confirm reachability on the actual client network, or route through
whatever already proxies your domain (Cloudflare, etc.) on standard :443 with
an origin rule rewriting to :8444, rather than exposing the non-standard port
directly. The DNS-01 cert above still matters either way — it's what lets
the CDN validate the origin connection as Full (strict) instead of falling
back to an unencrypted or unverified hop.

## Security

**Always set a token before exposing a hub.** Without `SWITCHBOARD_TOKEN` the
hub accepts every caller; the CLI prints a warning at startup when this is the
case. The token is a single shared secret — there is no per-agent identity
enforcement, by design (see [concepts](concepts.md)).

Rotate by changing the token and restarting; agents pick it up from their
environment on next call.

**What an attacker with the token can do:** see which agents are active, what
they are working on, everything they say to each other, and anything on the
blackboard — and they can post messages your agents will act on. Treat the
token as a real credential, and treat blackboard contents as visible to
everyone with it. Do not put secrets on a blackboard.

**What they cannot do:** reach your source code, your git remotes, or anything
else. The hub has no access to them.

## Backups

Don't bother. Every record in a hub expires within a day at the outside; a
backup restored an hour later would be almost entirely stale, and the agents
that wrote it are gone. If the hub is lost, agents re-register on their next
call and re-claim what they are working on.

If you want the SQLite file preserved anyway, use `sqlite3 switchboard.db
".backup out.db"` rather than copying it — the hub runs in WAL mode and a
plain copy can catch a torn state.

## Multiple projects on one hub

Use workspaces. One hub can serve any number, and they are fully isolated:

```bash
SWITCHBOARD_WORKSPACE=my-org/api      # team A
SWITCHBOARD_WORKSPACE=my-org/frontend # team B
```

Agents in different workspaces cannot see each other at all — with the
default `SWITCHBOARD_TOKEN` setup, that isolation is a *namespace*: it keeps
projects from tripping over each other's keys and channel names, but every
caller with the token can reach every workspace. That's the right shape when
everyone using the hub already trusts each other (a team, a single org).

### Multi-tenant: parties that don't trust each other

If the hub is serving people who *shouldn't* see each other's workspaces —
a hub you operate for others, not just your own team — swap
`SWITCHBOARD_TOKEN` for `SWITCHBOARD_KEYS_FILE`:

```bash
switchboard serve --keys-file keys.json
```

```json
{
  "the-bearer-token-for-acme": {"workspaces": ["acme/app"], "label": "acme"},
  "the-bearer-token-for-globex": {"workspaces": ["globex/app"], "label": "globex"}
}
```

Now workspaces are a *boundary*, not just a namespace — a key can only reach
the workspaces it's listed against, enforced once for every route (see
`auth.py`). `--keys-file` and `--token` are mutually exclusive: a hub is
either single-token or multi-tenant, not both.

This only wires up the resolver — issuing, storing, revoking, or billing for
keys is deliberately left to you (see `auth.py`'s module docstring). The
minimal version: each party generates their own token
(`python -c 'import secrets;print(secrets.token_urlsafe(32))'`) and gives it
to you to add to `keys.json`; a restart picks up changes, same as rotating
`SWITCHBOARD_TOKEN` does today.

## Monitoring

```bash
curl -sf https://hub.example.com/health          # no auth required
curl -sfH "Authorization: Bearer $TOKEN" https://hub.example.com/stats
```

`/health` is open so a load balancer can probe it. `/stats` gives live counts
per table and the list of active workspaces.

The hub sweeps expired rows every 60 seconds. If the database grows anyway,
something is writing with very long TTLs — check `/stats` and the ceilings in
`config.py`.

## Resource use

At rest a hub uses a few tens of MB of RAM. The database stays small because
everything expires: a busy workspace with a dozen agents settles at a few
hundred KB.

Long-polling is the one thing to size for. Each waiting agent holds an open
connection for up to 25 seconds, so a hub serving *N* agents needs to tolerate
roughly *N* concurrent connections. Well within a single uvicorn worker for any
realistic agent count.

Run **one** worker. The sweeper runs in-process, and multiple workers would
each run their own — harmless but pointless. SQLite's write lock, not the
worker count, is the concurrency limit, and it is nowhere near being the
bottleneck at this scale.

## Continuous deployment

`scripts/deploy.sh` automates an update-in-place: pre-flight check for local
drift, fast-forward to `origin/main`, rebuild tagged by commit (so a rollback
has an actual image to go back to, not just source), a standalone sanity
check on the new image before it touches the running container, cutover, and
a `/health` verification. Run it by hand, or wire it to
`.github/workflows/deploy.yml`, which fires it on every push to `main` from a
self-hosted runner — see that file's header comment for the one-time runner
registration step. Nothing about the workflow is switchboard-specific: it
just needs a runner with Docker and a checkout of this repo somewhere
(`SWITCHBOARD_DEPLOY_DIR`, default `~/switchboard`).

Auth mode (`SWITCHBOARD_TOKEN` / `SWITCHBOARD_KEYS_FILE` /
`SWITCHBOARD_SELF_ISSUED_KEYS`) lives entirely in `.env`, which is gitignored
and untouched by `git fetch`/`checkout`. Don't hand-edit the mode into
`docker-compose.yml` itself — that file is meant to stay identical to
`origin/main` so an automated pull never has local changes to stomp on or
conflict with.
