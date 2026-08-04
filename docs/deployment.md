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

Agents in different workspaces cannot see each other at all. Run separate hubs
only when you want separate *tokens* — that is the boundary a workspace does
not give you.

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
