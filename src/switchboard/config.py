"""Configuration for the Switchboard server and clients.

Everything is environment-driven so a hub can be stood up with no config file:

    SWITCHBOARD_DB           path to the SQLite file (server)
    SWITCHBOARD_TOKEN        shared bearer token (server + client)
    SWITCHBOARD_URL          hub base URL (client). Unset, the client dials
                             MANAGED_HUB_URL — the same hub `init` writes, so
                             a path that misses `.mcp.json` cannot silently
                             land somewhere else
    SWITCHBOARD_WORKSPACE    workspace to join (client). Unset, the client
                             derives one — see ``default_workspace`` and
                             docs/environments.md
    SWITCHBOARD_AGENT_ID     stable identity for this agent (client)
    SWITCHBOARD_KEY          workspace key for end-to-end encryption (client only —
                             a hub must never be given one, and has no use for it)


"""

from __future__ import annotations

import functools
import getpass
import hashlib
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import rooms

# --- TTL defaults (seconds) -------------------------------------------------
# Every record in Switchboard expires. These are the defaults applied when a
# caller does not pass an explicit ttl; each can be overridden per call, and
# the ceilings below bound what a caller is allowed to ask for.

DEFAULT_AGENT_TTL = 120
DEFAULT_LEASE_TTL = 900  # 15 minutes; renew via heartbeat
DEFAULT_MESSAGE_TTL = 3600  # 1 hour
DEFAULT_BOARD_TTL = 86400  # 24 hours

MAX_AGENT_TTL = 3600
MAX_LEASE_TTL = 86400
MAX_MESSAGE_TTL = 86400
MAX_BOARD_TTL = 7 * 86400

# How long an unused read cursor survives. Deliberately much longer than
# DEFAULT_AGENT_TTL: an agent's presence lapsing (a long turn, a gap between
# sessions) must not cost it its place in a conversation — only an agent that
# never reads at all for this long forfeits it. No message ever outlives this
# either, since MAX_MESSAGE_TTL is bounded well under it, so a cursor this old
# has nothing left to protect anyway.
DEFAULT_CURSOR_TTL = 7 * 86400  # 7 days

# Long-poll ceiling for `GET /inbox?wait=`. Kept under the 30s that most
# proxies use as an idle-read timeout.
MAX_WAIT_SECONDS = 25.0

# A waiting reader is woken directly by any write to a channel it cares about
# (see notify.py), so it does not poll in order to find messages. It still
# re-checks on this slow floor, because the notifier is in-process and cannot
# see writes made by another worker or another hub instance sharing the same
# database. Delivery correctness rests on this interval; the notifier only
# makes the common case fast. Lower it if you run multiple workers and care
# more about worst-case latency than about idle query load.
POLL_INTERVAL_SECONDS = 5.0

# How often the background sweeper hard-deletes expired rows. Reads already
# filter on expiry, so this is about reclaiming space, not correctness.
SWEEP_INTERVAL_SECONDS = 60.0


# --- where a client dials --------------------------------------------------

#: The hub a client talks to when nothing configured one, and what `init`
#: writes with no `--url`. These used to disagree: `init` defaulted here while
#: a client with no `.mcp.json` in reach defaulted to localhost, so any path
#: that missed that file dialled a private hub inside its own container and
#: coordinated with nobody, successfully and silently. One default, so the two
#: cannot drift apart again.
MANAGED_HUB_URL = "https://switchboard.lucille-ai.com"

#: The managed hub's access token, and deliberately not a secret. It ships with
#: the URL because it *is* reachability information: every client uses the same
#: value, nothing issues it, and publishing it here is the point.
#:
#: What it buys is that unauthenticated internet noise gets a 401 from a string
#: compare instead of a database query. Most scanning is untargeted, so that is
#: a real saving for no friction — nobody ever types this.
#:
#: What it does not buy is a boundary. Anyone who reads this file has it. A hub
#: that wants a real perimeter sets a secret token instead, and that one never
#: goes in a committed file — see `_init_mcp_json`.
#:
#: Lives here rather than in cli.py because the MCP bridge builds its client
#: straight from `from_env` and never imports the CLI. `ci.yml` and
#: `deploy.yml` sed it out of *this file*; tests/test_workflow_token.py pins
#: that extraction, so reformatting this line breaks a build rather than a hub.
MANAGED_HUB_TOKEN = "sb_public_lucille"  # noqa: S105 - published on purpose


def is_loopback(url: str) -> bool:
    """Whether `url` names a hub reachable only from the machine running it.

    Port-agnostic and scheme-agnostic on purpose: `--port 9000` does not make
    a loopback hub any more reachable from elsewhere.
    """
    host = urlsplit(url).hostname
    if not host:
        return False
    host = host.lower()
    return host == "localhost" or host == "::1" or host.startswith("127.")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class ServerConfig:
    """Server-side settings, read from the environment."""

    db_path: str = "switchboard.db"
    token: str | None = None
    sweep_interval: float = SWEEP_INTERVAL_SECONDS
    #: Queueing-delay target in milliseconds. Zero disables admission control
    #: entirely, which is the default: the target is meant to be chosen from
    #: measured p95 (see /stats "load"), and shipping a number here would be
    #: the invented constant #72 exists to avoid.
    load_target_ms: float = 0.0

    @classmethod
    def from_env(cls) -> ServerConfig:
        return cls(
            db_path=os.environ.get("SWITCHBOARD_DB", "switchboard.db"),
            token=os.environ.get("SWITCHBOARD_TOKEN") or None,
            sweep_interval=_env_float("SWITCHBOARD_SWEEP_INTERVAL", SWEEP_INTERVAL_SECONDS),
            load_target_ms=_env_float("SWITCHBOARD_LOAD_TARGET_MS", 0.0),
        )


def machine_suffix(extra: str = "") -> str:
    """A short, stable, opaque tag for this machine, for disambiguating a
    workspace name nobody chose.

    `extra` mixes in anything else that should make the tag differ — a
    checkout path, when there is one — so two unrelated directories both
    called `api` do not land on each other.

    An unconfigured client used to land in the workspace literally named
    ``default``. On a private hub that is harmless and even useful — two
    terminals on one laptop meet with no setup. On a shared hub it means every
    unconfigured user in the world is in one room, colliding over lease names
    and wondering who the strangers are. A key seals what they say, so this was
    never a disclosure, but it is a bad default.

    Hashing means the tag is unique without being *descriptive*: the workspace
    is the one thing the hub always sees in the clear, so a hostname would make
    it the most identifying string we hand over. Same inputs on the same
    machine give the same tag, so same-machine agents still find each other
    with no configuration — which is the property that made ``default`` useful
    in the first place.

    It is deliberately *not* stable across machines, and that is why it is
    only ever the fallback. Where there is a git remote to derive from,
    `default_workspace` uses it instead precisely so that clones on different
    machines do match; this tag is for when there is nothing shared to derive
    from at all, and two directories that merely happen to share a name are
    not a room anybody chose.
    """
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry, no USER, no LOGNAME
        user = ""
    seed = f"{socket.gethostname()}\0{user}\0{extra}"
    return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:8]


@functools.lru_cache(maxsize=128)
def _checkout_root(cwd: str) -> str:
    """The checkout containing `cwd`, or `cwd` itself when there is none.

    The project root rather than the working directory, so two terminals in
    different subdirectories of one project still agree — meeting with no
    configuration is the whole point of having a default at all.

    Found by walking up for a `.git` entry rather than by running
    `git rev-parse`. This is on the path of every client construction,
    including the lifecycle hooks that run at session start and stop, and
    spawning a process there costs more than the answer is worth. A `.git`
    that is a file rather than a directory — a worktree or submodule — counts
    as a root too, which is what we want: a worktree is a separate checkout
    and deserves its own room.
    """
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return cwd


def _git_common_dir(checkout: Path) -> Path | None:
    """The `.git` directory holding `checkout`'s config, or None.

    A `.git` file rather than a directory is a worktree or a submodule, and
    points at the real gitdir; a worktree's gitdir then names the common dir
    that actually holds `config`. Following both means every worktree of a
    repo derives the same remote, and so lands in the same room — which is
    what you want from agents working one repo on one branch each.
    """
    dot = checkout / ".git"
    if dot.is_dir():
        return dot
    if not dot.is_file():
        return None
    try:
        pointer = dot.read_text().strip()
    except OSError:
        return None
    if not pointer.startswith("gitdir:"):
        return None
    gitdir = Path(pointer.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (checkout / gitdir).resolve()
    common = gitdir / "commondir"
    if common.is_file():
        try:
            return (gitdir / common.read_text().strip()).resolve()
        except OSError:
            return None
    return gitdir


def git_remote_workspace(directory: Path | str | None = None) -> str | None:
    """`org/repo` from the checkout's origin remote, or None if there isn't one.

    Read out of `.git/config` rather than by running `git config --get`, for
    the reason `_checkout_root` walks the tree rather than running
    `git rev-parse`: this is on the path of every client construction,
    including the lifecycle hooks that run at session start and stop, and
    spawning a process there costs more than the answer is worth.

    One implementation, used by both `init` and the runtime default. They used
    to derive the workspace separately, which is the same shape of bug as the
    two hub defaults that disagreed: a repo where the two answers differ is a
    repo whose agents cannot find each other, and nothing would have said so.
    """
    root = Path(_checkout_root(str(Path(directory or os.getcwd()).resolve())))
    gitdir = _git_common_dir(root)
    if gitdir is None:
        return None
    try:
        text = (gitdir / "config").read_text()
    except OSError:
        return None
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != 'remote "origin"' or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip().lower() == "url":
            match = re.search(r"[:/]([^/:]+/[^/:]+?)(\.git)?$", value.strip())
            return match.group(1) if match else None
    return None


@functools.lru_cache(maxsize=128)
def _root_commits(checkout: str) -> str:
    """The checkout's root commit(s), or "" — a salt that is already committed.

    Every clone of a repo has the same root commit, and nobody who lacks read
    access to the repo can obtain it. That is exactly the shape of salt this
    needs: stable across machines, so two clones agree, and not derivable from
    the repo's *name*, so knowing where somebody works is not enough to guess
    the room they are in.

    A subprocess, unlike everything else on this path, because there is no
    cheap file to read: the root is found by walking parents, and the objects
    may be packed. Bounded to one call per checkout per process by the cache,
    and to nothing at all when there is no remote — `default_workspace` only
    asks once it already has a name to salt. Any failure returns "" and the
    caller falls back to the machine-scoped tag rather than to a guessable
    name.

    Sorted, so a history with two root commits (an unrelated history merged
    in) derives the same salt regardless of the order git happens to list
    them in.

    Memoised into the git directory as well as in-process, because finding a
    root means walking every commit to reach it — a second or more on a large
    repo, paid by a session-start hook that has no business being slow. The
    answer cannot change for an existing history, so the cache never needs
    invalidating; it lives under `.git`, so it is per-clone and never
    committed; and if it cannot be written (read-only checkout, odd
    permissions) the walk simply happens again.
    """
    cache = _git_common_dir(Path(checkout))
    cached = cache / "switchboard-root-commits" if cache else None
    if cached is not None:
        try:
            return cached.read_text().strip()
        except OSError:
            pass
    try:
        result = subprocess.run(
            ["git", "-C", checkout, "rev-list", "--max-parents=0", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    roots = " ".join(sorted(result.stdout.split()))
    if cached is not None and roots:
        try:
            cached.write_text(roots)
        except OSError:
            pass
    return roots


def default_workspace(directory: Path | str | None = None) -> str:
    """The workspace a client uses when nobody named one.

    Two properties have to hold at once, and either alone is a bad default.

    It must be *the same in every clone*, or the thing this is a default for
    never happens: a laptop, a cloud session and a CI runner on one repo have
    to land in one room without anybody configuring anything. A machine-scoped
    tag cannot do that.

    And it must not be *guessable*, because a name nobody chose is a room its
    occupants did not agree to be findable in. The workspace is the one value
    a hub sees in the clear when no key is set, so a plain `org/repo` would
    let anyone who knows where you work walk in.

    Hashing the repo's identity with its root commit gives both. The remote
    supplies the identity, the root commit the salt — already committed, in
    every clone, and unobtainable without read access to the repo. Same wire
    format as a declared room (see `rooms.workspace_for`), because it is the
    same kind of thing: an opaque identifier both sides derive rather than
    one either side assigns.

    `init` still writes the readable `org/repo` into `.mcp.json` and that file
    wins wherever it exists — a name you saw printed and chose to commit is a
    different act from one nothing announced. This is only what happens when
    nothing was written down.

    Falls back to the machine-scoped tag when there is no remote, or no commit
    to salt with: with nothing shared to derive from there is no cross-machine
    matching to be had, and an unsalted name is not worth having instead.
    """
    root = _checkout_root(str(Path(directory or os.getcwd()).resolve()))
    remote = git_remote_workspace(root)
    salt = _root_commits(root) if remote else ""
    if remote and salt:
        return rooms.workspace_for(f"{remote}\0{salt}")
    return f"default-{machine_suffix(root)}"


def _selected_room(directory: Path):
    """The room this repo and this environment agree on, or None.

    Deliberately swallows a resolution failure rather than raising here.
    `from_env` is on the path of every command, including ones that have
    nothing to do with a hub, so a malformed rooms file must not make
    `switchboard --help` fail. The failure surfaces where it is actionable —
    see `switchboard rooms`, which reports it in full.
    """
    try:
        declared = rooms.load(directory)
        if not declared:
            return None
        return rooms.select(declared)
    except rooms.RoomsError:
        return None


@dataclass
class ClientConfig:
    """Client-side settings, read from the environment."""

    url: str = MANAGED_HUB_URL
    #: Which tier of the resolution below actually supplied `url`. Carried
    #: rather than discarded because "nobody chose this" and "somebody chose
    #: exactly this" are the same string once the default is substituted, and
    #: telling them apart is the whole of `isolation_warning`. Kept in step by
    #: every later tier that overrides the URL — see the CLI's `_make_config`.
    url_source: str = "default"
    token: str | None = None
    workspace: str = field(default_factory=default_workspace)
    agent_id: str | None = None
    #: Workspace key for end-to-end encryption. When set, payloads are sealed
    #: and identifiers blinded before anything leaves this process. It is never
    #: transmitted; the hub cannot read the workspace with or without it.
    key: str | None = None
    #: Path to this agent's local timing-observations database (see timing.py).
    #: Purely local — never sent to the hub, never shared with other agents.
    timing_db: str = "~/.switchboard/timing.db"

    def effective_token(self) -> str | None:
        """The token to actually send, once the URL is finally known.

        The managed hub's token is published (see `MANAGED_HUB_TOKEN`), so a
        client that ends up there with nothing configured should use it rather
        than 401 — otherwise defaulting to that hub would leave the tool
        broken out of the box for anyone who never ran `init`.

        Deliberately a method rather than a default on `token`: the URL is
        settled in tiers, and the last one wins (`--url`, then `.mcp.json`).
        Baking the token in at `from_env` time would mean a config that
        started out managed and was later repointed at a self-hosted hub still
        carrying the public token — sending a credential somewhere nobody
        chose to send it. Resolving late means the two can never disagree.
        """
        if self.token:
            return self.token
        return MANAGED_HUB_TOKEN if self.url == MANAGED_HUB_URL else None

    @classmethod
    def from_env(cls, directory: Path | None = None) -> ClientConfig:
        """Resolve a client's settings.

        A repo that declares rooms (see rooms.py) supplies the workspace, the
        key and the hub together, as one record — which is what stops a key and
        a workspace disagreeing, since they are no longer two values that have
        to be kept in step.

        The environment still wins over the record, for the ordinary reason:
        it is how a cloud environment or a one-off command overrides what a
        checkout says. A repo with no rooms file behaves exactly as before.

        Each tier records *that* it supplied the URL, not just the value. The
        default is applied last and labelled, so a caller can still ask whether
        anyone chose this hub — see `url_source`.
        """
        url = os.environ.get("SWITCHBOARD_URL", "").rstrip("/") or None
        url_source = "env" if url else None
        workspace = os.environ.get("SWITCHBOARD_WORKSPACE") or None
        key = os.environ.get("SWITCHBOARD_KEY") or None

        where = Path.cwd() if directory is None else directory
        room = _selected_room(where)
        if room is not None:
            if url is None and room.hub_url:
                url, url_source = room.hub_url, "rooms"
            workspace = workspace or room.workspace
            key = key or rooms.key_for(room.key_id)

        return cls(
            url=url or MANAGED_HUB_URL,
            url_source=url_source or "default",
            token=os.environ.get("SWITCHBOARD_TOKEN") or None,
            # Resolved against the directory this config is *for*, not the
            # process's cwd: a hook or a bridge asking about a checkout it is
            # not sitting inside would otherwise derive somebody else's repo.
            workspace=workspace or default_workspace(where),
            agent_id=os.environ.get("SWITCHBOARD_AGENT_ID") or None,
            key=key,
            timing_db=os.environ.get("SWITCHBOARD_TIMING_DB", "~/.switchboard/timing.db"),
        )


def isolation_warning(config: ClientConfig, kind: str) -> str | None:
    """Text warning that this agent is talking to a hub nobody else can reach.

    The trap this catches is a repo whose committed hub URL is loopback —
    `init --local` on somebody's laptop — cloned into an environment that is
    not that laptop. Every signal stays green: the hub is reachable, register
    succeeds, and a roster containing only yourself looks exactly like being
    first to arrive. Nothing raises, so say it here.

    Two conditions, both cheap and both local — no probe, and nothing about
    whether peers happen to be online right now:

    * the URL is loopback and this is not a `local` agent, so a hub inside
      this container cannot be what anyone meant; and
    * the URL came from a committed file or from the default rather than from
      this environment. `--url http://127.0.0.1:8787` or an exported
      `SWITCHBOARD_URL` is somebody deciding on purpose, here, now — the one
      case a warning would be nagging rather than news. A CI job running a
      self-contained hub in-container is doing exactly that, and stays silent.
    """
    if kind == "local" or config.url_source in ("flag", "env"):
        return None
    if not is_loopback(config.url):
        return None
    origin = {
        "mcp.json": "the committed .mcp.json",
        "rooms": "this repo's rooms file",
    }.get(config.url_source, "the built-in default")
    corroboration = ""
    if config.token or config.key:
        # Not required — an unconfigured cloud agent is just as alone — but
        # when it is there it is the tell that somebody meant to coordinate,
        # and it makes the warning obviously about them rather than generic.
        secret = "SWITCHBOARD_TOKEN" if config.token else "SWITCHBOARD_KEY"
        corroboration = (
            f" {secret} is set here, so something in this environment expected "
            "to be talking to other agents."
        )
    return (
        f"warning: this {kind} agent's hub is {config.url}, which is reachable "
        f"only from inside this container. It came from {origin}, not from this "
        "environment.\n"
        "Agents anywhere else cannot reach it: your messages go nowhere, your "
        "leases exclude nobody, and the roster shows only you — which looks "
        f"exactly like being first to arrive.{corroboration}\n"
        "Set SWITCHBOARD_URL to a hub every agent can reach — see "
        "docs/environments.md."
    )


def clamp_ttl(ttl: float | None, default: float, maximum: float) -> float:
    """Resolve a caller-supplied ttl against its default and ceiling.

    A ttl of None means "use the default". Anything <= 0 is rejected by the
    API layer before reaching here, so this only guards the upper bound.
    """
    if ttl is None:
        return float(default)
    return float(min(ttl, maximum))
