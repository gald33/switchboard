"""Command-line interface for Switchboard.

Two audiences: humans looking at what their agents are doing, and shell hooks
wiring agents into a hub. Commands that agents call in hooks all accept
``--quiet`` and use exit codes, so they compose in scripts:

    switchboard claim db/migrations || exit 0   # someone else has it
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

import httpx

from . import __version__, drill, holds, invite, rendezvous, rooms
from .client import (
    WHISPER_TYPE,
    Client,
    Identity,
    LeaseHeld,
    SwitchboardError,
    detect_identity,
    identity_drift_warning,
)
from .config import (
    LOCAL_SETTINGS_FILE,
    MANAGED_HUB_TOKEN,
    MANAGED_HUB_URL,
    PUBLISHED_PAGE_URL,
    ClientConfig,
    git_remote_workspace,
    is_loopback,
    is_private_page,
    isolation_warning,
    local_setting,
    machine_suffix,
    mcp_env,
    rooms_warning,
)
from .crypto import CryptoError, generate_key
from .guidance import SKILL_NAME, WAKE_SCRIPT_NAME, skill_history, skill_text, wake_script_body
from .spec import (
    OUTCOMES,
    PROVENANCES,
    SPEC_FILE,
    Refresh,
    Spec,
    SpecError,
    cadence,
    roles_for,
)
from .spec import Field as SpecField
from .timing import (
    EFFORT_LEVELS,
    MIN_SAMPLES,
    Forecast,
    TimingModel,
    declare_safely,
    note_look_safely,
    note_speak_safely,
    sender_forecast,
    unwrap_forecast,
    wrap_forecast,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 2


# --- output helpers ---------------------------------------------------------


def _use_color(stream: Any) -> bool:
    return stream.isatty() and not os.environ.get("NO_COLOR")


def _can_prompt(*, no_input: bool, quiet: bool, as_json: bool) -> bool:
    """Whether it is safe to stop and ask the operator a question.

    A prompt is only ever an improvement for a human at a keyboard. Every
    other caller — an agent's shell, CI, `init | tee`, a docs copy-paste —
    would hang on it, so the default has to be silence and the detection has
    to be something those callers can't accidentally look like. Both a TTY on
    stdin (there is someone to answer) and on stderr (they can see the
    question) are required; `--json` and `-q` opt out by construction, since
    a caller parsing output is not one that can answer.

    stdout is deliberately *not* checked: `init > log` from a terminal is
    still a human session, and the prompts go to stderr precisely so that
    redirect keeps working.
    """
    if no_input or quiet or as_json:
        return False
    # Some CI runners allocate a pseudo-TTY, which would defeat the check
    # above and hang the job on a question nobody can answer.
    if os.environ.get("CI"):
        return False
    return sys.stdin.isatty() and sys.stderr.isatty()


def _copy_to_clipboard(text: str) -> str | None:
    """Put `text` on the system clipboard, returning the tool used or None.

    Best effort by design: there is no clipboard at all over SSH or in a
    container, and failing to find one is not an error worth reporting as one
    — the value has already been printed, so nothing is lost.
    """
    candidates = [
        (["pbcopy"], "pbcopy"),                                  # macOS
        (["wl-copy"], "wl-copy"),                                # Wayland
        (["xclip", "-selection", "clipboard"], "xclip"),         # X11
        (["xsel", "--clipboard", "--input"], "xsel"),
        (["clip.exe"], "clip.exe"),                              # WSL
    ]
    for argv, name in candidates:
        if shutil.which(argv[0]) is None:
            continue
        try:
            proc = subprocess.run(argv, input=text.encode(), timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return name
    return None


def _ask(question: str, *, default: bool) -> bool:
    """Ask a yes/no question on stderr. EOF or an empty line takes `default`."""
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        print(f"{question} {suffix} ", end="", file=sys.stderr, flush=True)
        try:
            answer = input().strip().lower()
        except EOFError:
            print(file=sys.stderr)
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  answer y or n.", file=sys.stderr)


def _ask_choice(question: str, options: Sequence[tuple[str, str]], *, default: int) -> int:
    """Ask the operator to pick one of `options` (label, detail), returning its index."""
    print(question, file=sys.stderr)
    for i, (label, detail) in enumerate(options, start=1):
        marker = "*" if i - 1 == default else " "
        print(f"  {marker} {i}) {label} — {detail}", file=sys.stderr)
    while True:
        print(f"pick 1-{len(options)} [{default + 1}] ", end="", file=sys.stderr, flush=True)
        try:
            answer = input().strip()
        except EOFError:
            print(file=sys.stderr)
            return default
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print(f"  answer with a number from 1 to {len(options)}.", file=sys.stderr)


class Fmt:
    def __init__(self, color: bool) -> None:
        self.color = color

    def _wrap(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def dim(self, t: str) -> str:
        return self._wrap(t, "2")

    def bold(self, t: str) -> str:
        return self._wrap(t, "1")

    def green(self, t: str) -> str:
        return self._wrap(t, "32")

    def yellow(self, t: str) -> str:
        return self._wrap(t, "33")

    def red(self, t: str) -> str:
        return self._wrap(t, "31")

    def cyan(self, t: str) -> str:
        return self._wrap(t, "36")


def _ago(iso_ts: str | None) -> str:
    """Render an ISO timestamp as a compact relative age."""
    if not iso_ts:
        return "-"
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return iso_ts
    delta = (datetime.now(timezone.utc) - then).total_seconds()
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _body_text(body: Any) -> str:
    body, _ = unwrap_forecast(body)
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# --- command implementations ------------------------------------------------


def _add_timing_args(p: argparse.ArgumentParser) -> None:
    """The two semantic judgments a model supplies for adaptive timing.

    Deliberately not time estimates: the model says what kind of work it is
    about to do and roughly how big, and the local model converts that into
    seconds from this agent's own history (docs/adaptive-timing.md).
    """
    p.add_argument("--execution-class", metavar="LABEL",
                   help="short label for the work ahead, e.g. coding, research. "
                        "Free-form — the taxonomy emerges from use")
    p.add_argument("--effort", choices=EFFORT_LEVELS,
                   help="rough size of the work ahead")


def _apply_repo_url(config: ClientConfig, directory: Path) -> None:
    """Layer the committed `.mcp.json` hub URL onto `config`, in place.

    Only below the environment: an explicitly exported value is a deliberate
    override of whatever the checkout says.

    Its own function because `serve` has to answer the same question a client
    does — "where would a client started here dial?" — and a second copy of
    this precedence is a second answer that can disagree with the first.
    """
    if os.environ.get("SWITCHBOARD_URL"):
        return
    # Kept in step with the value, not inferred afterwards: by the time
    # anything reads this, `config.url` is a plain string and every tier that
    # could have set it looks identical. See `ClientConfig.url_source`.
    from_mcp = _mcp_url(directory)
    if from_mcp:
        config.url, config.url_source = from_mcp, "mcp.json"


#: Where a token came from, said the way somebody would fix it.
_TOKEN_SOURCES = {
    "flag": "the --token flag",
    "env": "SWITCHBOARD_TOKEN in this shell",
    "settings.local.json": ".claude/settings.local.json in this checkout",
    "mcp.json": ".mcp.json in this checkout",
    "invite": "the invite you passed",
}


def _for_report(args: argparse.Namespace) -> ClientConfig | None:
    """The configuration a failed command was using, for the message about it.

    Resolved from the same arguments rather than remembered from the run. A
    module global would be one process-wide value shared by everything in it —
    which is exactly how the first version of this leaked a stale config
    between two tests, and would have leaked one between two `main()` calls in
    anything embedding this CLI.
    """
    try:
        return _make_config(args)
    except Exception:
        return None


def _tried(config: ClientConfig | None) -> str:
    """One line naming the hub and where its credential came from.

    Both halves are the answer to a real question that the error alone cannot
    settle. `invalid or missing bearer token` reads identically whether nothing
    was sent, a stale export was, or a repo default was — and a token is an
    opaque string, so the value on screen would not distinguish them either.
    A connection failure has the same shape: what fixes it is knowing which URL
    was dialled, especially when it is a loopback default nobody chose.
    """
    if config is None:
        return ""
    origin = _TOKEN_SOURCES.get(config.token_source)
    credential = f"a token from {origin}" if origin else "no token at all"
    where = {
        "flag": "the --url flag", "env": "SWITCHBOARD_URL",
        "mcp.json": ".mcp.json", "rooms": "this repo's rooms file",
        "invite": "the invite", "default": "the built-in default",
    }.get(config.url_source, config.url_source)
    return f"  tried {config.url} (from {where}) with {credential}."


def _make_config(args: argparse.Namespace) -> ClientConfig:
    """Resolve where this command talks to, and as whom.

    Flags beat the environment, and the environment beats what the repo
    declares — `.mcp.json` for the hub and workspace `init` wired up, and
    `.claude/settings.local.json` for a token belonging to this machine
    rather than the checkout.

    That last tier used to exist only inside `whoami`, so `init` would wire
    a repo up and every *other* command still dialled the default localhost
    and died on connection refused. `whoami` reported a configuration
    nothing else used, which is the worst version of this: the one command
    you would check to find out looked healthy.

    The repo tiers now live in `ClientConfig.from_repo`, because the CLI is
    no longer the only thing standing in a checkout asking this question —
    `switchboard_viewer/viewer.py` asks it too, and a second implementation of a
    precedence order is a second answer that can disagree with the first.

    The workspace key is deliberately NOT resolved from the repo here, which
    is `from_repo`'s `include_secrets=False`. Claude Code injects
    `settings.local.json` into the agents it spawns, but a plain shell has
    nothing exported and would send in the clear — so reading it would make
    `whoami` claim a channel is sealed when this invocation would not seal
    it. A reader is a different case and says so explicitly. See `cmd_whoami`
    and `ClientConfig.from_repo`.
    """
    config = ClientConfig.from_repo(Path(".").resolve())

    if args.url:
        config.url, config.url_source = args.url.rstrip("/"), "flag"
    if args.token:
        config.token, config.token_source = args.token, "flag"
    if args.workspace:
        config.workspace = args.workspace
    if getattr(args, "key", None):
        config.key = args.key
    config = _apply_invite(config, args)
    return _apply_lobby(config, args)


def _apply_lobby(config: ClientConfig, args: argparse.Namespace) -> ClientConfig:
    """Fold `--lobby` in: the room every holder of this key already shares.

    Below `--invite`, which is a room somebody chose to hand over, and above
    everything else — like `--invite`, naming a room is the point of the flag,
    so a workspace from the environment must not quietly win.

    The key has to be resolvable *here*, and unlike everywhere else in this
    file that includes the repo's own `settings.local.json`: the lobby is
    derived from the key, so without one there is no room to compute rather
    than a room we would join in the clear. Saying that out loud is the whole
    value — the alternative is joining some other room and finding it empty.
    """
    if not getattr(args, "lobby", False):
        return config
    if getattr(args, "invite", None):
        raise SystemExit(
            "--lobby and --invite name different rooms; pass one. An invite is "
            "a room somebody handed you, a lobby is the one your key already has."
        )
    key = config.key or _saved_key(Path(".").resolve())
    if not key:
        raise SystemExit(
            "--lobby needs a workspace key, because the lobby is derived from "
            "it — there is no lobby to compute without one.\n"
            "  Export SWITCHBOARD_KEY, pass --key, or run `switchboard init "
            "--new-key` to mint one."
        )
    config.key = key
    config.workspace = rooms.lobby(key).workspace
    return config


def _apply_invite(config: ClientConfig, args: argparse.Namespace) -> ClientConfig:
    """Fold `--invite` in, above every other tier.

    An invite outranks the environment on purpose, and it is the one tier
    where that is a safety property rather than a convenience. An agent with a
    `SWITCHBOARD_KEY` exported — which is most of them, because `init` exports
    one — that is handed a room and reads its own key anyway does not fail:
    it joins the workspace it was pointed at, on a key nobody there holds, and
    sits in a room that looks quiet. Anything less than "the invite wins" makes
    `--invite` a suggestion, and a suggestion cannot be relied on to move an
    agent into somebody else's room.

    A flag that contradicts the invite is refused rather than merged. The four
    values are a room; overriding one of them describes a room nobody is in,
    and the whole reason invites exist is that arriving in one fails silently.
    Agreeing with the invite is fine — a script that passes both is not
    confused about which room it means.
    """
    blob = getattr(args, "invite", None)
    if not blob:
        return config
    room = invite.Invite.decode(blob)
    clashes = [
        flag
        for flag, given, carried in (
            ("--url", (args.url or "").rstrip("/"), room.url.rstrip("/")),
            ("--token", args.token, room.token),
            ("--workspace", args.workspace, room.workspace),
            ("--key", getattr(args, "key", None), room.key),
        )
        if given and given != carried
    ]
    if clashes:
        raise invite.InviteError(
            f"{', '.join(clashes)} disagrees with --invite. An invite is a whole "
            "room — hub, workspace, token and key travel together — so overriding "
            "one of them describes a room nobody is in. Drop the flag, or drop "
            "the invite."
        )
    return replace(
        config, url=room.url.rstrip("/"), url_source="invite",
        workspace=room.workspace,
        # A field the invite omits means "you already hold this" — that is
        # what `invite --no-key` and `--no-token` are *for* — so it falls back
        # rather than clearing. Wiping a key the environment has would send an
        # agent into an encrypted room in the clear, which is the silent
        # failure again wearing the fix's clothes.
        token=room.token or config.token,
        # A named key it did not carry is looked up rather than guessed at, and
        # its absence is refused — see `Invite.resolve_key`.
        key=room.resolve_key(config.key),
    )


#: Set once the drift warning has been printed, so a command that builds two
#: clients (`whoami` does) says it once rather than twice.
_DRIFT_WARNED = False


def _make_client(args: argparse.Namespace) -> Client:
    config = _make_config(args)
    identity = detect_identity(agent_id=args.agent_id)
    # Here rather than per-command: every command that reaches the hub comes
    # through this function, and the warning is about the identity it is about
    # to speak under. Wiring it into the handful of commands that publish would
    # leave the ones that only read — `agents`, `inbox` — silently reading a
    # second identity's cursor.
    _warn_identity_drift(args, identity)
    _warn_unresolved_room(args, config)
    return Client(config, agent_id=identity.agent_id)


#: Set once the rooms warning has been printed, so a command building two
#: clients says it once. Same shape as `_DRIFT_WARNED` next door.
_ROOMS_WARNED = False


def _warn_unresolved_room(args: argparse.Namespace, config: ClientConfig) -> None:
    """Say, on stderr, that this command is about to use a room the repo's own
    file did not choose.

    Here rather than in the commands that publish, for the same reason the
    identity warning is: a command that only reads — `agents`, `inbox` — is
    reading the wrong room just as thoroughly, and an empty answer from it is
    the most convincing version of the failure.
    """
    global _ROOMS_WARNED
    if _ROOMS_WARNED or getattr(args, "quiet", False):
        return
    note = rooms_warning(config)
    if note:
        _ROOMS_WARNED = True
        print(note, file=sys.stderr)


def _warn_isolated(
    args: argparse.Namespace,
    config: ClientConfig | None = None,
    kind: str | None = None,
) -> None:
    """Say, on stderr, that this agent is dialling a hub nobody else can reach.

    Stderr and exit 0, unlike the key-mismatch warning next door in
    `cmd_agents`, which exits non-zero so a hook notices. The difference is
    that a mismatched key is never intentional, while a CI job running a
    self-contained hub inside its own container is a real and legitimate
    setup — failing it would be the regression. `isolation_warning` already
    keeps quiet when the URL was chosen here; this only has to not shout over
    `--quiet`.
    """
    if getattr(args, "quiet", False):
        return
    if config is None:
        config = _make_config(args)
    if kind is None:
        kind = detect_identity(agent_id=getattr(args, "agent_id", None)).kind
    note = isolation_warning(config, kind)
    if note:
        print(note, file=sys.stderr)


def _warn_identity_drift(args: argparse.Namespace, identity: Identity | None = None) -> None:
    """Say, on stderr, that this command is publishing under a second identity.

    Rides alongside `_warn_isolated` because it is the same class of failure:
    every call succeeds, and the damage is that you are not who you think you
    are to everybody else. Stderr and exit 0 for the same reason — running a
    command from outside a checkout is legitimate, it just should not be
    silent when it changes who you are.
    """
    global _DRIFT_WARNED
    if getattr(args, "quiet", False) or _DRIFT_WARNED:
        return
    if identity is None:
        identity = detect_identity(agent_id=getattr(args, "agent_id", None))
    note = identity_drift_warning(identity)
    if note:
        _DRIFT_WARNED = True
        print(note, file=sys.stderr)


# --- adaptive timing --------------------------------------------------------
#
# The client-side half of adaptive timing (docs/adaptive-timing.md), which
# until now only the MCP bridge could reach. The model supplies two cheap
# judgments — `--execution-class` and `--effort` — and everything downstream
# (history, quantiles, self-correction) is timing.py's job.
#
# One thing works differently here than in the bridge, and it is the whole
# reason this needed more than argument plumbing. The bridge is a live
# process: it declares and later observes its own look, so a forecast window
# lives in memory-adjacent state owned by one run. A CLI run is a *sequence
# of processes* — `dm --effort high` exits, and the `inbox` that closes that
# window is a different process entirely. timing.py drops a window whose
# runtime does not match, precisely so a crashed agent's downtime is not
# learned as behaviour, so under CLI use every observation would be discarded
# and the estimator would sit on its bootstrap priors forever.
#
# So the CLI names its run explicitly instead of inheriting process identity.


def _runtime_id(agent_id: str) -> str:
    """Identify the *run* a CLI declaration belongs to.

    `SWITCHBOARD_RUNTIME_ID` lets a caller scope this deliberately — export
    it once per script and every command in that script shares one window,
    while a second concurrent script gets its own.

    Without it the fallback is stable per agent, so the common case (a loop
    of plain CLI calls) learns rather than silently discarding everything.
    The cost is that a window left open by an abandoned run is closed by
    whatever looks next, turning downtime into one long observation — which
    is why timing.py's 24h ceiling and its `dropped` counter matter here:
    such an observation is rejected and counted, not averaged in.
    """
    return os.environ.get("SWITCHBOARD_RUNTIME_ID") or f"cli:{agent_id}"


class _Timing:
    """Declare/observe helper for one CLI command.

    Every method swallows its errors. A forecast is advisory — a corrupt or
    unwritable local store should cost a hint, never the coordination call
    the user actually made. Same contract as `Bridge._declare`.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        config = _make_config(args)
        self.workspace = config.workspace
        self.agent_id = detect_identity(agent_id=args.agent_id).agent_id
        self.execution_class = getattr(args, "execution_class", None)
        self.effort = getattr(args, "effort", None)
        self._model: TimingModel | None = None
        self._db_path = config.timing_db

    @property
    def model(self) -> TimingModel | None:
        if self._model is None:
            try:
                self._model = TimingModel(
                    self._db_path, runtime_id=_runtime_id(self.agent_id))
            except Exception:
                return None
        return self._model

    def note_look(self) -> None:
        note_look_safely(self.model, self.agent_id, self.workspace)

    def note_speak(self) -> None:
        note_speak_safely(self.model, self.agent_id, self.workspace)

    def declare(self) -> Forecast | None:
        return declare_safely(
            self.model, self.agent_id, self.workspace,
            self.execution_class, self.effort)

    def close(self) -> None:
        if self._model is not None:
            try:
                self._model.close()
            except Exception:
                pass


# The envelope and the sender's view of a forecast are one implementation in
# timing.py, shared with the MCP bridge — see the wire-contract section there
# for why these stopped being two. This file used to alias all three to
# private names that said what they did; timing.py now says it itself, and a
# local synonym for a shared contract is one more name to keep in step.


def _forecast_line(fmt: Fmt, forecast: dict[str, Any], subject: str) -> str:
    """One or two dim lines: when someone next expects to look, and — when
    they predicted it — when they next expect to speak.

    Kept as separate lines because they answer different questions. "When
    will they see this?" is the look; "when will they reply?" is the speak,
    and a peer trying to act at the same moment needs the second one.
    """
    line = fmt.dim(
        f"  {subject} looking again ~{_until(forecast.get('p50'))} (p50), "
        f"~{_until(forecast.get('p95'))} (p95)"
    )
    if forecast.get("speak_p50"):
        line += "\n" + fmt.dim(
            f"  {subject} speaking again ~{_until(forecast.get('speak_p50'))} (p50), "
            f"~{_until(forecast.get('speak_p95'))} (p95)"
        )
    return line


def _until(iso: Any) -> str:
    """Render a forecast checkpoint as a countdown. Past checkpoints say so
    rather than showing a negative: past p95 the predicted event has almost
    certainly already happened, and the forecast is spent."""
    if not isinstance(iso, str):
        return "?"
    try:
        from datetime import datetime, timezone
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = (then - datetime.now(timezone.utc)).total_seconds()
    except ValueError:
        return "?"
    return _dur(delta) if delta > 0 else "already due"


def _dialable_url(host: str, port: int) -> str:
    """The URL a client on this machine should use for a hub bound here.

    `0.0.0.0` and `::` mean "every interface", which is a binding instruction
    and not an address anything dials — handing it to a client as-is is a
    connection refused with a confusing cause.
    """
    if host in ("0.0.0.0", "::", ""):  # noqa: S104 - matching, not binding
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _would_reach(url: str, host: str, port: int) -> bool:
    """Whether `url` reaches a hub bound to `host`:`port` on this machine."""
    parts = urlsplit(url)
    if (parts.port or (443 if parts.scheme == "https" else 80)) != port:
        return False
    if not parts.hostname:
        return False
    if host in ("0.0.0.0", "::", ""):  # noqa: S104 - matching, not binding
        # Bound everywhere, so anything that resolves here reaches it. Only
        # loopback is decidable without a DNS lookup, which is not worth doing
        # to phrase a hint.
        return is_loopback(url)
    return parts.hostname.lower() == host.lower() or (
        is_loopback(url) and is_loopback(f"http://{host}")
    )


def _serve_client_note(url: str, host: str, port: int) -> str | None:
    """What to tell someone starting a hub that their own clients will miss.

    A client with nothing configured dials the managed hub, so `serve` in one
    terminal and an agent in another no longer meet by default — the agent
    reaches something real, which is exactly why it never complains. This is
    the one moment we know both halves: what is being started, and what a
    client standing in this directory would dial instead.

    Silent when they already agree, which covers `SWITCHBOARD_URL` exported
    for the shell and a repo that committed a local hub with `init --local`.
    """
    if _would_reach(url, host, port):
        return None
    return (
        f"note: agents started here will dial {url}, not this hub. Point them "
        f"at it:\n    export SWITCHBOARD_URL={_dialable_url(host, port)}\n"
        "or commit it for the whole repo with `switchboard init --local`."
    )


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "the server needs extra dependencies: pip install 'agent-switchboard[server]'",
            file=sys.stderr,
        )
        return EXIT_ERROR
    from .config import ServerConfig
    from .server import create_app

    config = ServerConfig.from_env()
    if args.db:
        config.db_path = args.db
    if args.token:
        config.token = args.token
    if args.cors_origin:
        config.cors_origins = tuple(args.cors_origin)

    if not config.token:
        # The one thing worth saying loudly at startup: with no token this hub
        # admits anyone who can reach it. Rooms are still unguessable and still
        # sealed, but nothing stops a stranger addressing one they learn.
        print(
            "warning: no token set — this hub admits any caller. Fine on "
            "localhost; set --token/SWITCHBOARD_TOKEN for anything reachable "
            "from elsewhere.",
            file=sys.stderr,
        )

    # Flushed because the note below goes to stderr, which is unbuffered: to a
    # terminal both are line-buffered and interleave correctly, but redirected
    # to a log file this line would otherwise sit in a buffer until exit and
    # the note would appear to precede the hub it is about.
    print(f"switchboard {__version__} → http://{args.host}:{args.port}  db={config.db_path}",
          flush=True)
    if not getattr(args, "quiet", False):
        client = ClientConfig.from_env()
        _apply_repo_url(client, Path(".").resolve())
        note = _serve_client_note(client.url, args.host, args.port)
        if note:
            print(note, file=sys.stderr)
    uvicorn.run(
        create_app(config), host=args.host, port=args.port,
        log_level=args.log_level,
    )
    return EXIT_OK


def _keygen_invite(args: argparse.Namespace, key: str, workspace_token: str) -> int:
    """Hand a freshly minted room over as one string.

    A side channel and an invited room stopped being two ideas once an invite
    could carry a whole room: the only difference is where the coordinates came
    from — minted here, or received from someone who minted them. So minting
    should produce the same artifact as inviting, and this is that. The hub and
    the token come from wherever this invocation was already pointed, because a
    side room is a room on *your* hub; only the workspace and the key are new.

    A probe is sealed if the hub can be reached, so the far side can verify
    rather than assume. If it cannot, the invite is still emitted — an
    unverifiable room beats no room, and saying so is cheaper than failing.
    `keygen` without this flag stays what it was: purely local, no hub, works
    before anything is registered.
    """
    workspace = rooms.workspace_for(workspace_token)
    config = replace(_make_config(args), workspace=workspace, key=key)
    fmt = Fmt(_use_color(sys.stdout))
    probe, unproven = f"coord/join-probe/{secrets.token_urlsafe(8)}", ""
    try:
        with Client(config, agent_id=f"keygen-{secrets.token_hex(4)}") as room:
            room.board_set(probe, invite.PROBE_SENTINEL, ttl=86400)
    except (SwitchboardError, OSError, CryptoError) as exc:
        probe, unproven = "", str(exc)

    blob = invite.Invite(
        url=config.url, workspace_token=workspace_token, token=config.token,
        key=key, note=args.note or "", probe=probe,
    )
    handover, caveat = _handover(blob, args)
    if args.json:
        _print_json({
            "invite": blob.encode(), "describes": blob.redacted(),
            "key": key, "workspace": workspace,
            "workspace_token": workspace_token,
            "verifiable": bool(probe),
            **({"link": handover} if args.link else {}),
            **({"unproven": unproven} if unproven else {}),
        })
        return EXIT_OK

    print(handover)
    if caveat:
        print(f"\n{caveat}", file=sys.stderr)
    if unproven:
        print(f"\n{fmt.yellow('no proof-of-room')} — could not reach the hub to "
              f"leave one ({unproven}).\nThe room is still usable; the far side "
              "will not be able to verify it.", file=sys.stderr)
    if _can_prompt(no_input=getattr(args, "no_input", False), quiet=args.quiet,
                   as_json=args.json):
        print(
            f"\n{blob.redacted()}\n\n"
            + fmt.yellow("This is a credential.")
            + " Nobody is in this room yet — it exists once\nyou hand this to the "
              "peers you want in it, and to nobody else.\n\n"
            + ("They open it in a browser." if args.link else
               "They run:  switchboard --invite <the string above> say build hi\n"
               "      or:  switchboard join <the string above>"),
            file=sys.stderr,
        )
        _offer_clipboard(handover, "the invite", args)
    return EXIT_OK


def cmd_keygen(args: argparse.Namespace) -> int:
    """Print a fresh workspace key, and an opaque workspace name to go with it.

    The workspace name is the one thing a hub sees in the clear — it is the
    shard and routing key, so it cannot be encrypted. But nothing requires it
    to be *meaningful*, and "acme/billing" tells an operator more than any
    other single string they hold. Emitting an opaque one here is the cheapest
    privacy win available, and it costs nothing to take.
    """
    key = generate_key()
    # A token, with the identifier derived from it — not an identifier invented
    # directly. Both are opaque and the same shape on the wire, so this costs
    # nothing, and it is the difference between a room that can be written into
    # a rooms file afterwards and one that can only ever be retyped: the file
    # records rooms by token, because `hash(token)` does not run backwards.
    workspace_token = generate_key()
    workspace = rooms.workspace_for(workspace_token)

    if getattr(args, "as_invite", False):
        return _keygen_invite(args, key, workspace_token)

    if args.json:
        _print_json({"key": key, "workspace": workspace})
        return EXIT_OK
    print(key)
    if sys.stdout.isatty():
        print(
            "\nShare this with the agents in the workspace and nobody else:\n"
            f"  export SWITCHBOARD_KEY={key}\n"
            "\nThe hub never receives it, so it cannot read this workspace — and\n"
            "cannot help you recover it either. Everything expires within a day,\n"
            "so losing it costs less here than almost anywhere else.\n"
            "\nThe workspace name is NOT encrypted — it is how the hub routes.\n"
            "Use an opaque one and it stops being the most descriptive thing\n"
            "the hub holds. Keep the readable name in your own notes:\n"
            f"  export SWITCHBOARD_WORKSPACE={workspace}",
            file=sys.stderr,
        )
    return EXIT_OK


#: One implementation of "what did `init` leave in this repo", in config.py,
#: where `ClientConfig.from_repo` needs it too. These names stay because
#: `init` reads better with them.
_saved_setting = local_setting


def _saved_key(directory: Path) -> str | None:
    return _saved_setting(directory, "SWITCHBOARD_KEY")


def _env_block(
    url: str, workspace: str, key: str | None, token: str | None, *, all_four: bool
) -> str:
    """What a second environment has to be told, ready to paste.

    The secrets only, by default. The URL and the workspace live in the
    committed `.mcp.json`, so a checkout already has them — setting them in
    the environment as well pins that machine to values it should be reading
    from the repo, and it then keeps the old ones when the repo moves. That is
    the same class of silent divergence as a mismatched key, arrived at by
    being helpful.

    `all_four` is for the environment that genuinely has no checkout to read
    from (see docs/environments.md), where nothing else supplies them.

    Omits what this machine does not know rather than emitting a blank, which
    would override a correct value already set at the far end.
    """
    pairs = [("SWITCHBOARD_KEY", key), ("SWITCHBOARD_TOKEN", token)]
    if all_four:
        pairs = [("SWITCHBOARD_URL", url), ("SWITCHBOARD_WORKSPACE", workspace)] + pairs
    return "\n".join(f"{name}={value}" for name, value in pairs if value)


def _offer_clipboard(text: str, what: str, args: argparse.Namespace) -> None:
    """On a terminal, offer to copy. Defaults to yes: the only reason to run
    this is to move the value somewhere else, and it is already on screen, so
    the clipboard adds no exposure the command has not already caused."""
    if not _can_prompt(
        no_input=getattr(args, "no_input", False), quiet=args.quiet, as_json=args.json
    ):
        return
    if not _ask(f"\ncopy {what} to the clipboard?", default=True):
        return
    tool = _copy_to_clipboard(text)
    if tool:
        print(f"copied ({tool})", file=sys.stderr)
    else:
        print("no clipboard tool found — copy it from above", file=sys.stderr)


def cmd_rooms(args: argparse.Namespace) -> int:
    """Show what this repo declares and what this environment can open.

    `ClientConfig.from_env` deliberately swallows a bad rooms file — it runs on
    the path of every command, including ones with nothing to do with a hub, so
    a malformed file must not break `--help`. This is where that failure is
    supposed to surface, in full, with the reason.
    """
    directory = Path(args.dir or ".").resolve()
    try:
        declared = rooms.load(directory)
    except rooms.RoomsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if not declared:
        print(
            f"no rooms declared in this repo ({rooms.ROOMS_FILE} is absent), so the "
            "workspace comes from the environment or is derived — see "
            "`switchboard whoami`."
        )
        return EXIT_OK

    selected = None
    try:
        selected = rooms.select(declared)
    except rooms.RoomsError as exc:
        problem = str(exc)
    else:
        problem = None

    if args.json:
        _print_json({
            "rooms": [
                {"name": r.name, "key_id": r.key_id, "hub_url": r.hub_url,
                 "private": r.private, "have_key": bool(rooms.key_for(r.key_id)),
                 "selected": selected is not None and r.name == selected.name}
                for r in declared
            ],
            "selected": selected.name if selected else None,
            "problem": problem,
        })
        return EXIT_OK if selected else EXIT_ERROR

    fmt = Fmt(_use_color(sys.stdout))
    for room in declared:
        have = bool(rooms.key_for(room.key_id))
        mark = (fmt.green("*") if selected and room.name == selected.name
                else (" " if have else fmt.dim("·")))
        note = "" if have else fmt.dim(f"  (no {rooms.env_var_for(room.key_id)})")
        where = fmt.dim(" private") if room.private else ""
        print(f"  {mark} {room.name}{where}  key={room.key_id}{note}")
    if problem:
        print()
        print(fmt.yellow("Note: ") + problem)
    return EXIT_OK if selected else EXIT_ERROR


def cmd_help(args: argparse.Namespace) -> int:
    """Print the coordination protocol.

    `--help` describes the commands; this describes the *convention* — when to
    claim, what a forecast means, where a handoff goes. An agent that has the
    CLI but no installed skill has no other way to reach it, and that is the
    common case rather than the odd one: a repo nobody ran `init` in, a
    harness with no skill mechanism, a session that has hit something
    confusing and needs the convention now.

    Deliberately touches neither the hub nor the network. It is most useful
    exactly when something is wrong, which is the worst moment to require a
    working connection to read the instructions.
    """
    text = skill_text()
    overlay = ""
    role = getattr(args, "role", None)
    if role:
        roles = roles_for(Path(".").resolve())
        if role not in roles:
            # An error, not a quiet fall back to the shared protocol. An agent
            # that believes it received role guidance and got boilerplate is
            # the confidently-wrong case, and it is the same reasoning that
            # makes `drill --worker claude` refuse rather than downgrade when
            # the binary is missing.
            known = ", ".join(sorted(roles)) or "none recorded"
            print(
                f"error: this repo declares no role {role!r} (known: {known}).\n"
                "Roles come from .switchboard/spec.json — Switchboard defines "
                "none of its own. `switchboard refresh set` records them.",
                file=sys.stderr,
            )
            return EXIT_ERROR
        overlay = str(roles[role] or "").strip()
    if args.json:
        payload = {"skill": _SKILL_NAME, "text": text}
        if role:
            payload["role"] = role
            payload["overlay"] = overlay
        print(json.dumps(payload, indent=2))
    else:
        print(text, end="" if text.endswith("\n") else "\n")
        if overlay:
            print(f"\n---\n\n## Your role here: {role}\n\n{overlay}")
    return EXIT_OK


def _dur_ago(seconds: float) -> str:
    return _dur(max(0.0, seconds)) + " ago"


def cmd_refresh(args: argparse.Namespace) -> int:
    """Read or update what this repo declares about how its agents work.

    `init` is the human-invoked half — it wires a repo up once. This is the
    agent-invoked half, for the answers that are discovered rather than
    configured and that go stale as the repo moves. Same output medium on
    purpose: a committed file, versioned with the code that made it true and
    present in a fresh clone, rather than a blackboard entry that expires in a
    day and is unreadable to anyone on another key.
    """
    root = Path(getattr(args, "dir", None) or ".").resolve()
    try:
        spec = Spec.load(root)
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.refresh_action == "status":
        stale = spec.is_stale(root)
        gap = cadence(spec.history)
        last = spec.last
        if args.json:
            _print_json({
                "path": str(root / SPEC_FILE),
                "fields": {n: f.as_json() for n, f in spec.fields.items()},
                "inputs": spec.inputs,
                "stale": stale,
                "fingerprint": spec.fingerprint(root),
                "mean_seconds_between_changes": gap,
                "history": [r.as_json() for r in spec.history],
            })
            return EXIT_OK
        fmt = Fmt(_use_color(sys.stdout))
        if not spec.fields and not spec.history:
            print(f"no spec recorded in this repo ({SPEC_FILE} is absent).")
            print("`switchboard refresh set` writes one; Switchboard supplies "
                  "no fields of its own.")
            return EXIT_OK
        for name, f in sorted(spec.fields.items()):
            mark = fmt.dim(f"[{f.provenance}]")
            print(f"{name:<20} {mark} {json.dumps(f.value)[:80]}")
        print()
        if last:
            print(f"last refresh  {last.outcome} {_dur_ago(time.time() - last.at)}"
                  + (f" — {last.note}" if last.note else ""))
        # Three answers, and the third is not a failure: with nothing declared
        # to compare against, saying so beats a confident "fresh" that would
        # stop anyone ever looking again.
        verdict = {
            True: fmt.yellow("inputs have changed since that refresh"),
            False: fmt.green("inputs unchanged since that refresh"),
            None: fmt.dim("no inputs declared — cannot tell from here; "
                          "judge by the cadence below"),
        }[stale]
        print(f"staleness     {verdict}")
        if gap is not None:
            print(f"cadence       something changed every {_dur(gap)} on average")
        return EXIT_OK

    # --- set ---
    raw = args.spec_json
    if raw == "-" or raw is None:
        raw = sys.stdin.read().strip()
    if not raw:
        print("error: no spec given; pass JSON as an argument or `-` for stdin",
              file=sys.stderr)
        return EXIT_ERROR
    try:
        incoming = json.loads(raw)
    except ValueError as exc:
        print(f"error: spec is not valid JSON: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not isinstance(incoming, dict):
        print("error: spec must be a JSON object of field name -> value",
              file=sys.stderr)
        return EXIT_ERROR

    updates = {
        name: SpecField(value=value, provenance=args.provenance,
                        evidence=args.evidence or "")
        for name, value in incoming.items()
    }
    try:
        changed = spec.apply(updates, force=args.force)
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.input:
        spec.inputs = sorted(set(spec.inputs) | set(args.input))

    refused = sorted(set(updates) - set(changed))
    outcome = args.outcome or ("updated" if changed else "unchanged")
    try:
        spec.record(Refresh(
            at=time.time(), outcome=outcome, note=args.note or "",
            fingerprint=spec.fingerprint(root),
            by=detect_identity(agent_id=args.agent_id).agent_id,
        ))
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    path = spec.save(root)

    if args.json:
        _print_json({"path": str(path), "changed": changed, "unchanged": refused,
                     "outcome": outcome, "fingerprint": spec.fingerprint(root)})
        return EXIT_OK
    print(f"{path}: {len(changed)} field(s) written, outcome {outcome}")
    for name in changed:
        print(f"  + {name}")
    for name in refused:
        # Silence here would be the bug: an agent that inferred a value, was
        # correctly refused, and heard nothing would report having recorded it.
        existing = spec.fields.get(name)
        why = ("already asserted — pass --force to overwrite"
               if existing and existing.provenance == "asserted"
               else "unchanged")
        print(f"  = {name} ({why})")
    return EXIT_OK


def cmd_rendezvous(args: argparse.Namespace) -> int:
    """Find a peer you have never met, and leave something behind if you do not.

    One invocation is a bounded look, not a vigil: announce, read the notes
    others left, poll on an escalating backoff for as long as was budgeted,
    write your own note, and report the shared slot to come back at. A
    turn-based agent cannot hold a socket for an hour, and the failure this
    command exists to fix is precisely two agents each deciding to stop.
    """
    fmt = Fmt(_use_color(sys.stdout))
    identity = detect_identity(agent_id=args.agent_id)
    config = _make_config(args)
    # An agent parked with capacity has no topic to name, and one arriving with
    # a task cannot know the name the helper would have chosen. Falling back to
    # the reserved topic is what lets those two meet at all.
    topic = args.topic or rendezvous.OPEN_TOPIC
    role = rendezvous.OFFERING if args.offer else rendezvous.SEEKING
    blurb = args.offer if args.offer else (args.want or "")
    key = rendezvous.key_for(topic)
    look = args.wait if args.wait is not None else rendezvous.DEFAULT_LOOK_SECONDS

    with _make_client(args) as hub:
        agent = hub.register(
            name=identity.name, kind=identity.kind, branch=identity.branch,
            task=args.task or (
                f"available: {blurb}" if role == rendezvous.OFFERING
                else f"rendezvous: {topic}"
            ),
            channels=args.channel or [], meta=identity.meta,
            back_in=args.back_in,
        )
        now = rendezvous.hub_now(agent)
        slot = rendezvous.next_slot(config.workspace, topic, now)

        found: list[dict[str, Any]] = []
        strangers: list[dict[str, Any]] = []
        peers: list[rendezvous.Intent] = []
        # Only the reserved topic needs the roles kept apart. Two agents who
        # agreed a topic out of band have already established that they are
        # about the same thing, and are usually both seeking — filtering there
        # would hide each from the other, which is the very meeting the named
        # topic was agreed to arrange.
        want_role = (
            rendezvous.complement(role) if topic == rendezvous.OPEN_TOPIC else None
        )
        for gap in rendezvous.schedule(look):
            if gap:
                time.sleep(gap)
            # Notes first, and every pass rather than once at the end. Presence
            # alone is the wrong stop condition for an agent that is offering:
            # the room it is parked in may be full of other helpers, and
            # breaking on the first live body would end the search before the
            # one seeker it exists to serve had written anything down.
            peers = _rendezvous_notes(
                hub, topic, now, exclude=hub.agent_id, role=want_role,
            )
            if peers:
                break
            everyone = hub.agents()
            # A peer whose name will not open is on a different key, which is
            # the one person in the room you have *not* met: you will both sit
            # in each other's roster and be unable to exchange a word. Counting
            # them as found is this command's own failure mode, reproduced —
            # it would report a meeting and stop the search that could still
            # find a real one. They are reported instead, below.
            strangers = hub.key_mismatches(everyone)
            stranger_ids = {a.get("agent_id") for a in strangers}
            roster = [
                a for a in everyone
                if a.get("agent_id") != hub.agent_id
                and a.get("agent_id") not in stranger_ids
            ]
            live = [a for a in roster if not a.get("away")]
            # Presence answers a seeker's question — anyone here is someone to
            # ask. It does not answer an offering agent's, which is not "is
            # anybody about" but "does anybody need me", and only a note says
            # that. So an offer keeps looking and lets the budget run out.
            if role == rendezvous.SEEKING or topic != rendezvous.OPEN_TOPIC:
                if live:
                    found = live
                    break
                if roster:
                    # Nobody at the keyboard, but somebody said they are coming
                    # back. That is a meeting, just not this second — and it is
                    # the answer an empty roster could never give.
                    found = roster
                    break
            else:
                found = live or roster

        # Whether each peer can actually be woken, rather than merely intends
        # to look. A note is a plan; a live `listener/<id>` is a process saying
        # so now, and it decides whether a DM lands in seconds or waits for
        # their next turn.
        parked = rendezvous.reachable_now(
            hub.board_list(prefix=rendezvous.LISTENER_PREFIX)
        ) if peers else set()

        mine = rendezvous.Intent(
            agent_id=hub.agent_id, topic=topic, want=blurb,
            since=now, looking_until=now + (args.until or rendezvous.SLOT_SECONDS * 6),
            next_slot=slot, role=role,
        )
        hub.board_set(key + "/" + hub.agent_id, mine.as_json())

    if args.json:
        _print_json({
            "topic": topic, "agent_id": mine.agent_id, "role": role,
            "roster": found,
            "notes": [
                {**n.as_json(), "reachable": n.agent_id in parked} for n in peers
            ],
            "next_slot_in": round(slot - now, 1),
            # For a seeker, anyone present is someone to ask, so presence
            # counts. For an agent offering capacity it does not: the others in
            # the room are usually more helpers, and calling that a meeting is
            # the same false positive as matching your own role.
            "met": bool(peers) if role == rendezvous.OFFERING else bool(found or peers),
            # Separate from `roster` on purpose: a caller that treats these as
            # peers has reintroduced the bug. They are here so the mismatch is
            # actionable rather than invisible.
            "key_mismatches": [a.get("agent_id") for a in strangers],
        })
        return EXIT_OK

    if found:
        awake = [a for a in found if not a.get("away")]
        for a in found:
            state = "here" if a in awake else f"away {_dur(a.get('back_in') or 0)}"
            print(f"{fmt.green('found')}  {a['agent_id'][:33]:<34} {state}")
    label = "offer" if role == rendezvous.SEEKING else "needs"
    for note in peers:
        state = "parked" if note.agent_id in parked else "between turns"
        print(f"{fmt.green(label)}  {note.agent_id[:33]:<34} "
              f"{note.want[:48] or '(no description)':<49} {fmt.dim(state)}")
    if peers:
        # The note is the introduction, not the conversation. Say the next move
        # explicitly: an agent that has found a peer and does not know it may
        # now simply address it will go back to waiting on the board.
        first = peers[0]
        print(
            f"\nThat is a peer, not a thread. DM the id with what you actually "
            f"need —\n`switchboard dm {first.agent_id[:33]} \"…\"` — and take the "
            f"work off this topic;\nthe reserved one is everybody's."
        )
        if first.agent_id in parked:
            # Worth saying explicitly: a parked listener wakes on delivery, so
            # the sender should expect an answer rather than plan a return.
            print("A listener is parked for it, so a DM wakes it within seconds.")
        else:
            print(
                "No listener is parked for it, so a DM is correct but silent "
                "until its\nnext turn — do not wait on a reply this turn."
            )
    if strangers:
        # Never presented as a near miss. This is the forty-minute failure
        # exactly: same hub, same workspace, different key, both listed. The
        # roster is the thing that cannot tell you, so say it here and point
        # at the command that proves it either way.
        print(
            f"\n{fmt.yellow('key mismatch')} — {len(strangers)} agent(s) here are on a "
            f"different workspace key.\nYou are in each other's roster and cannot "
            f"exchange anything. If one of\nthem is who you came for, this is the "
            f"whole problem: get an invite and\nrun `switchboard join <invite>`, "
            f"which fails loudly instead of quietly."
        )
    if not found and not peers:
        # The honest report, and the one that must not read as failure: an
        # agent told "nobody is here" stops, which is how both sides quit.
        print(f"{fmt.dim('nobody yet')} — your note is at {key}/{hub_id_hint(mine)}")
    print(
        f"\nnext shared slot in {_dur(slot - now)}. Both sides derive it from the "
        f"workspace,\nso come back then and your peer will be looking too."
    )
    return EXIT_OK


#: Where an arriving agent writes what it came to do. Under `coord/` so the
#: convention's own first move — read that prefix — finds it.
ARRIVE_PREFIX = "coord/agents/"


def cmd_arrive(args: argparse.Namespace) -> int:
    """Announce, read EVERY durable surface, and leave something that outlives you.

    The command exists because "check the room" had no defined meaning, and two
    agents proved on 2026-08-17 that this is not pedantry. One checked the
    roster and the channel list; the other checked its inbox. Both concluded the
    room was empty. It was not: a board entry had been sitting in it for
    twenty-four minutes, and neither of them read the board.

    An inbox check in a room you just joined is the worst of these, because it
    can only ever come back empty — nobody has yet sent a message to an id you
    have not published. Answering "is anybody here?" with it is asking a
    question that has one possible answer.

    So this reads all three, and — the part that matters — **names what it
    checked**. "Nobody is here" and "I looked in one place" must not render the
    same, which is the same rule the claim audit follows when it cannot reach
    the roster.

    The write is not a courtesy either. Presence lapses in two minutes and the
    hub is designed to be cheap to lose, so a handoff between sessions that do
    not overlap cannot live in presence. It lives in the board entry this
    leaves, which outlasts the turn that wrote it by a day.
    """
    fmt = Fmt(_use_color(sys.stdout))
    identity = detect_identity(agent_id=args.agent_id)
    intent = args.intent or "(no intent given)"

    with _make_client(args) as hub:
        agent = hub.register(
            name=identity.name, kind=identity.kind, branch=identity.branch,
            task=intent, channels=args.channel or [], meta=identity.meta,
            back_in=args.back_in,
        )
        now = rendezvous.hub_now(agent)

        roster = [a for a in hub.agents() if a.get("agent_id") != hub.agent_id]
        strangers = hub.key_mismatches(hub.agents())
        stranger_ids = {a.get("agent_id") for a in strangers}
        peers = [a for a in roster if a.get("agent_id") not in stranger_ids]
        board = [e for e in hub.board_list(prefix="coord/")
                 if e.get("key") != ARRIVE_PREFIX + hub.agent_id]
        waiting = hub.inbox(peek=True, limit=20)

        hub.board_set(
            ARRIVE_PREFIX + hub.agent_id,
            {"agent_id": hub.agent_id, "branch": identity.branch,
             "name": identity.name, "intent": intent, "since": now},
        )

    surfaces = {"roster": len(peers), "board": len(board), "inbox": len(waiting)}
    if args.json:
        _print_json({
            "intent": intent, "agent_id": hub.agent_id, "checked": surfaces,
            "roster": peers, "board": board, "inbox": waiting,
            "key_mismatches": [a.get("agent_id") for a in strangers],
            "alone": not (peers or board or waiting),
        })
        return EXIT_OK

    for a in peers:
        state = f"away {_dur(a.get('back_in') or 0)}" if a.get("away") else "here"
        print(f"{fmt.green('peer')}   {str(a.get('agent_id'))[:33]:<34} {state}  "
              f"{(a.get('task') or '')[:40]}")
    for e in board:
        print(f"{fmt.green('board')}  {str(e.get('key'))[:33]:<34} "
              f"{_dur(e.get('expires_in') or 0)} left")
    for m in waiting:
        print(f"{fmt.green('inbox')}  #{m.get('seq')} from {str(m.get('from'))[:26]}")

    if strangers:
        print(f"\n{fmt.yellow('key mismatch')} — {len(strangers)} agent(s) here hold a "
              f"different workspace key. You are in each other's roster and cannot "
              f"exchange anything; `switchboard join <invite>` settles it either way.")

    # The line this command exists for. An empty room and an unchecked one are
    # different findings, and only one of them justifies going quiet.
    print(f"\nchecked {surfaces['roster']} peer(s) on the roster, "
          f"{surfaces['board']} note(s) under coord/, "
          f"{surfaces['inbox']} unread message(s).")
    if not (peers or board or waiting):
        print("Every durable surface is empty — this room really is unused, not "
              "merely unchecked.")
    print(f"Your intent is on the board at {ARRIVE_PREFIX}{hub.agent_id[:12]}… and "
          f"outlives this turn; presence does not.")
    return EXIT_OK


def hub_id_hint(intent: rendezvous.Intent) -> str:
    return intent.agent_id[:33]


def _rendezvous_notes(
    hub: Client, topic: str, now: float, *, exclude: str, role: str | None = None
) -> list[rendezvous.Intent]:
    """Live intent left by anyone else on this topic.

    Notes whose author has given up are dropped rather than shown: sending a
    newcomer to wait on somebody who stopped hours ago is the same wasted turn
    this command exists to prevent.

    ``role`` keeps an unequal meeting unequal. On the reserved topic every
    agent holding the key writes here, and most of them are the same kind as
    you — a helper reading other helpers' offers has found a crowd, not a
    peer. Ask for the complement of your own role and the crowd resolves into
    the few notes you can actually act on.
    """
    out = []
    for entry in hub.board_list(prefix=rendezvous.key_for(topic)):
        if entry.get("unreadable"):
            continue
        note = rendezvous.Intent.from_json(entry.get("value"))
        if not note or note.agent_id == exclude or not note.still_looking(now):
            continue
        if role is not None and note.role != role:
            continue
        out.append(note)
    return sorted(out, key=lambda n: n.since)



def cmd_whoami(args: argparse.Namespace) -> int:
    identity = detect_identity(agent_id=args.agent_id)
    config = ClientConfig.from_env()
    directory = Path(".").resolve()
    # `encrypted` describes what *this* invocation will do, so it deliberately
    # ignores the key sitting in the repo: Claude Code injects that file's env
    # into the agents it spawns, but a plain shell has nothing exported and
    # will send in the clear. Claiming otherwise is the overclaim that gets
    # someone to trust a channel that isn't sealed.
    #
    # Asked of the client that would do the sealing rather than recomputed
    # from the same inputs, for the reason `addressed_as` is taken from it
    # below: two implementations of "will this be sealed?" can disagree, and
    # the one that reports is not the one that acts.
    hub = _make_client(args)
    encrypted = hub.encrypted
    key = args.key or config.key or _saved_key(directory)
    # `config.workspace` falls back to the literal "default", so it can never
    # be None and would mask the repo's real workspace. Read the environment
    # directly to tell "explicitly set" from "defaulted", and prefer what
    # .mcp.json routes to over a placeholder — handing a teammate `-w default`
    # is the same silent misroute this file works to prevent.
    # A flag that *names a room* outranks all of this, and has to, or `whoami`
    # prints the repo's room while every other command in the same invocation
    # talks to the one the flag chose. That is precisely the failure the
    # comment below describes, one flag later: the command you run to find out
    # where you are pointed, pointing somewhere else.
    resolved_room = _make_config(args)
    if getattr(args, "lobby", False) or getattr(args, "invite", None):
        workspace = resolved_room.workspace
    else:
        workspace = (
            args.workspace
            or os.environ.get("SWITCHBOARD_WORKSPACE")
            or _mcp_workspace(directory)
            or config.workspace
        )
    # Resolved through the path every *other* command uses rather than off
    # `config` directly. The payload below used to read `config.url`, which is
    # `from_env` alone and skips `.mcp.json` — so in an `init`-ed repo `whoami`
    # printed localhost while `announce`, one line later, talked to the
    # committed hub, and `--env` printed a third answer. The one command you
    # run to find out where you are pointed was the one that did not know.
    resolved = _make_config(args)
    url = resolved.url
    token = args.token or config.token or _saved_setting(directory, "SWITCHBOARD_TOKEN")
    if args.show_key or args.env:
        # Only ever on request. Printing a key into scrollback, CI logs or a
        # screen share is the kind of thing that should take a deliberate flag.
        if not key:
            print(
                "error: no key here — none set in the environment and "
                f"no {_LOCAL_SETTINGS_REL} in this directory. `switchboard init "
                "--new-key` mints one.",
                file=sys.stderr,
            )
            return EXIT_ERROR
        if args.json:
            _print_json({"key": key, "workspace": workspace})
            return EXIT_OK
        if args.env:
            # Exactly what a second environment needs, in the shape its own
            # config file wants — assembling these by hand from four places is
            # where a wrong workspace or a stale token creeps in.
            block = _env_block(url, workspace, key, token, all_four=args.no_repo)
            print(block)
            if _can_prompt(
                no_input=getattr(args, "no_input", False), quiet=args.quiet,
                as_json=args.json,
            ):
                if args.no_repo:
                    print(
                        f"\nAll four, for an environment with no checkout. Where there "
                        f"is one, drop --no-repo: {url} and {workspace} come from the "
                        "committed .mcp.json, and setting them by hand pins that machine "
                        "to values it should be following.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "\nThe secrets only. A checkout gets the hub URL and workspace "
                        "from the committed .mcp.json, so setting those by hand would "
                        "pin that machine to values it should be following. For an "
                        "environment with no checkout at all, use --no-repo.",
                        file=sys.stderr,
                    )
            _offer_clipboard(block, "these settings", args)
            return EXIT_OK
        print(key)
        if _can_prompt(
            no_input=getattr(args, 'no_input', False), quiet=args.quiet,
            as_json=args.json,
        ):
            print(
                f"\nGive teammates:  switchboard init --key {key} -w {workspace}\n"
                "For another machine of your own, `--env` prints a block you can "
                "paste straight into its environment.",
                file=sys.stderr,
            )
            _offer_clipboard(key, "the key", args)
        return EXIT_OK
    # The id peers actually address, which is not `identity.agent_id` once a
    # workspace key is in play: everything that leaves this machine is
    # blinded, so a collaborator sees the blinded form and `whoami` was
    # answering "how am I addressed?" with a name nobody can route to. Taken
    # from the client rather than blinded again here, so there is one
    # implementation of that mapping. Identical to the local id when there is
    # no key, which is why this went unnoticed.
    addressed_as = hub.agent_id
    payload = {
        "agent_id": addressed_as,
        "local_agent_id": identity.agent_id,
        "name": identity.name,
        "kind": identity.kind,
        "branch": identity.branch,
        "workspace": workspace,
        "hub": url,
        "encrypted": encrypted,
        "id_source": identity.id_source,
        "in_repo": identity.in_repo,
        "meta": identity.meta,
    }
    if args.json:
        _print_json(payload)
        # Still worth saying under --json: a script parsing this is exactly
        # the caller that will not notice an empty roster by eye. Stderr, so
        # the document on stdout stays byte-identical.
        _warn_isolated(args, resolved, identity.kind)
        return EXIT_OK
    fmt = Fmt(_use_color(sys.stdout))
    for field in ("agent_id", "name", "kind", "branch", "workspace", "hub"):
        value = payload[field]
        # Gated on `--quiet` as well as on the condition: the annotation points
        # at the stderr note, and pointing at something that was suppressed is
        # worse than saying nothing.
        if (field == "hub" and not args.quiet
                and isolation_warning(resolved, identity.kind)):
            # Annotated in place rather than printed as a block on its own:
            # the claim being qualified is this line, and a reader scanning
            # six fields should not have to join them up. Deliberately does
            # not say "see below" — the detail goes to stderr, and stdout to
            # a pipe is block-buffered, so the two do not reliably interleave
            # in the order they were written.
            value = f"{value} {fmt.dim('(this container only — see warning on stderr)')}"
        print(f"{fmt.dim(field.rjust(10))}  {value}")
    if addressed_as != identity.agent_id:
        # Only worth a line when they differ, which is exactly when someone
        # would otherwise hand out the wrong one. The annotation says what the
        # local id *is* as well as what it is not: it used to read only "this
        # machine only — peers use agent_id above", which is true and steered
        # readers off the one field that can confirm an identity pin. Blinding
        # means a pin that worked and a pin that was dropped both render as an
        # opaque token up there, so "did my --agent-id take?" is unanswerable
        # from the addressed id alone. It is answerable from this one.
        blinded_note = fmt.dim(
            "(what agent_id above is the blinded form of; peers cannot route to it)"
        )
        print(f"{fmt.dim('local'.rjust(10))}  {identity.agent_id} {blinded_note}")
    # Where the id came from. A wrong answer here is the difference between one
    # agent and two: an unpinned id is derived from repo + branch + session, so
    # the same session speaking from a different directory publishes under a
    # second identity, and two sessions sharing a branch publish under one.
    source = {
        "argument": "pinned (--agent-id)",
        "SWITCHBOARD_AGENT_ID": "pinned (SWITCHBOARD_AGENT_ID)",
    }.get(identity.id_source, "derived from repo + branch + session")
    if identity.id_source == "derived" and not identity.in_repo:
        source += fmt.yellow("  ← no git checkout here; see warning on stderr")
    print(f"{fmt.dim('identity'.rjust(10))}  {source}")
    print(f"{fmt.dim('encrypted'.rjust(10))}  "
          + (fmt.green("yes — the hub cannot read this workspace")
             if encrypted else "no"))
    # Last, and after `encrypted` on purpose: a sealed channel to a hub with
    # nobody else on it reads as reassurance, and this is the line that says
    # what that reassurance is worth.
    _warn_isolated(args, resolved, identity.kind)
    return EXIT_OK


def cmd_register(args: argparse.Namespace) -> int:
    identity = detect_identity(agent_id=args.agent_id)
    config = _make_config(args)
    with Client(config, agent_id=identity.agent_id) as hub:
        agent = hub.register(
            name=args.name or identity.name,
            kind=args.kind or identity.kind,
            branch=identity.branch,
            task=args.task,
            # None rather than []: a re-announce that says nothing about
            # channels should not remove them. `-c` with nothing after it is
            # how you clear them deliberately.
            channels=args.channel or None,
            meta=identity.meta,
            ttl=args.ttl,
            back_in=getattr(args, "back_in", None),
        )
    if args.json:
        _print_json(agent)
    elif not args.quiet:
        # The hub URL belongs on this line. Registering against a shared hub
        # and registering against one nobody else can reach printed the same
        # sentence, and this is the moment the agent declares itself present —
        # so it is the moment the claim is either true or a lie.
        print(f"registered {agent['agent_id']} ({agent['kind']}) in "
              f"{agent['workspace']} on {config.url}")
    _warn_isolated(args, config, identity.kind)
    return EXIT_OK


def _warn_unreadable(items: list[dict[str, Any]], noun: str, fmt: Fmt) -> None:
    """Say how many entries in a listing were sealed to a key we do not hold.

    The client keeps these rather than failing the call, so the listing is now
    honest but incomplete — and incomplete is the half that has to be said out
    loud. A silently short list reads as "nothing else is here", which is the
    same wrong conclusion the empty-roster trap produces, one layer down.
    """
    hidden = sum(1 for item in items if item.get("unreadable"))
    if not hidden:
        return
    print(
        fmt.yellow(
            f"note: {hidden} {noun} sealed to a different key and "
            f"could not be read."
        )
        + "\nRun `switchboard agents` to see who is on another key.",
        file=sys.stderr,
    )


def cmd_agents(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        agents = hub.agents()
        mismatched = hub.key_mismatches(agents)
    if args.json:
        _print_json(agents)
        return EXIT_OK
    if not agents:
        print("no agents registered")
        return EXIT_OK
    fmt = Fmt(_use_color(sys.stdout))
    print(fmt.bold(f"{'AGENT':<34} {'KIND':<7} {'BRANCH':<24} {'SEEN':<10} TASK"))
    swapped = []
    away = []
    for a in agents:
        seen = _ago(a["last_seen_at"])
        # Three states, not two. `away` is the agent's own statement that it is
        # between turns and returning, which is what an arriving peer needs and
        # what an empty roster could never say.
        if a.get("away"):
            back = a.get("back_in")
            seen_txt = fmt.dim(
                f"away {_dur(back)}" if isinstance(back, (int, float)) and back > 0
                else "away"
            )
            away.append(a["agent_id"])
        else:
            seen_txt = fmt.yellow(seen) if a.get("stale") else seen
        marker = fmt.red(" !") if a.get("key_changed_while_live") else ""
        if marker:
            swapped.append(a["agent_id"])
        row = (
            f"{a['agent_id'][:33]:<34} {a['kind'][:6]:<7} "
            f"{(a.get('branch') or '-')[:23]:<24} {seen_txt:<10} "
            f"{a.get('task') or ''}{marker}"
        )
        print(fmt.dim(row) if a.get("away") else row)
    if away and len(away) == len(agents):
        # The case worth spelling out: nobody is at the keyboard, but this is
        # not an empty room. An arriving agent that reads "no agents" gives up;
        # one that reads "everyone is between turns, back in ~4m" waits or
        # leaves a note, which is the whole point of the state existing.
        print(
            fmt.dim(
                "\nNobody is active this second, but every agent above said it "
                "is coming back.\nThis is not an empty room — leave what you "
                "need them to see rather than\nconcluding they are gone."
            ),
            file=sys.stderr,
        )
    if swapped:
        # The client sets this flag and, until now, nothing ever displayed it.
        # Registration upserts on (workspace, agent_id) and nothing validates
        # it, so this is what a second agent announcing over a live one looks
        # like from outside — which is the shape of a plain id collision as
        # much as of impersonation, and the two are worth the same look.
        print(
            "\n" + fmt.red(
                f"warning: {len(swapped)} agent(s) announced under an id that "
                f"was already live here, with a different signing key:"
            )
            + "\n  " + "\n  ".join(swapped)
            + "\nEither two agents derived the same id — sessions sharing a "
              "host, repo and\nbranch do, and a spawned agent inherits its "
              "parent's session — or something\nis announcing as somebody "
              "else. Pin SWITCHBOARD_AGENT_ID to rule out the first.",
            file=sys.stderr,
        )
    if mismatched:
        # A key mismatch is otherwise completely silent: their messages never
        # reach this inbox, this agent's never reach theirs, and neither
        # side's leases exclude the other. Nothing raises, so say it here.
        print(
            "\n" + fmt.red(
                f"warning: {len(mismatched)} agent(s) here hold a DIFFERENT "
                f"key."
            )
            + "\nYou cannot see their messages and they cannot see yours, and "
              "your leases\ndo not exclude each other. Check SWITCHBOARD_KEY "
              "matches on every agent.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    return EXIT_OK


def cmd_claim(args: argparse.Namespace) -> int:
    fmt = Fmt(_use_color(sys.stdout))
    with _make_client(args) as hub:
        standing = holds.declared_hold(hub, args.resource)
        if standing and not args.json:
            print(
                f"{fmt.yellow('declared')} — {holds.holder(standing)} says: "
                f"{str(standing.get('intent') or 'no reason given')[:70]}\n"
                f"  That is a standing declaration on the board, not a lease, so it "
                f"does not stop you — it is the thing a lease cannot say: somebody "
                f"means to keep this past their current turn. Check with them, or "
                f"go ahead knowing you were told.",
                file=sys.stderr,
            )
        try:
            lease = hub.acquire(args.resource, note=args.note, ttl=args.ttl)
        except LeaseHeld as exc:
            if args.json:
                _print_json({"acquired": False, **exc.payload})
            elif not args.quiet:
                print(
                    fmt.yellow(f"held by {exc.holder} for another {_dur(exc.expires_in)}"),
                    file=sys.stderr,
                )
            return EXIT_CONFLICT
        declared = False
        if args.declare:
            try:
                holds.declare(hub, args.resource, intent=args.note or "",
                              since=lease.get("acquired_at"))
                declared = True
            except SwitchboardError as exc:
                # The enforced half succeeded, so this is not a failed claim and
                # must not be reported as one. It IS a claim quieter than you
                # asked for, which you have to hear about.
                if not args.json:
                    print(fmt.yellow(f"claimed, but the declaration did not stick: {exc}"),
                          file=sys.stderr)
    if args.json:
        _print_json({"acquired": True, "declared": declared,
                     "standing_hold": standing, **lease})
    elif not args.quiet:
        print(fmt.green(f"claimed {lease['resource']} for {_dur(lease['expires_in'])}"))
        if declared:
            print(f"declared at {holds.HOLDS_PREFIX}{args.resource} — outlives the lease by "
                  f"design, so `release` it when you are actually done")
    return EXIT_OK


def cmd_renew(args: argparse.Namespace) -> int:
    """Extend one lease without touching the others.

    `checkin` renews everything you hold, which is usually right. This is for
    when it is not: you are still on one resource and want the rest to lapse
    for whoever is waiting on them.
    """
    with _make_client(args) as hub:
        lease = hub.renew(args.resource, ttl=args.ttl)
        unread = hub.unread_dms
    if args.json:
        _print_json({**lease, "unread_dms": unread})
    elif not args.quiet:
        print(f"renewed {args.resource} for {lease.get('expires_in')}s")
        _print_unread(unread)
    return EXIT_OK


def cmd_leave(args: argparse.Namespace) -> int:
    """Drop off the roster deliberately rather than fading on a timeout.

    Worth doing rather than just exiting: a peer waiting on you learns now
    instead of after your presence expires, and a roster listing only agents
    actually present is worth more to whoever reads it next.
    """
    with _make_client(args) as hub:
        target = None
        if args.agent:
            # Verbatim, and only if the roster is actually showing it. An id
            # read off the roster has already been blinded; blinding it again
            # is what made a ghost unretirable — three attempts across three
            # drifted identities, each told "was not on the roster" while the
            # roster printed exactly that id.
            roster = {a.get("agent_id") for a in hub.agents()}
            if args.agent not in roster:
                print(f"error: {args.agent} is not on this roster. `agents` "
                      "lists the ids this command accepts; a local id, a "
                      "branch or a name is not one of them.", file=sys.stderr)
                return EXIT_ERROR
            target = args.agent
        removed = hub.deregister(agent_id=target)
    if args.json:
        _print_json({"left": removed, "agent_id": target or hub.agent_id})
    elif not args.quiet:
        who = f" {target}" if target else ""
        print(f"left the roster{who}" if removed
              else f"was not on the roster{who}")
    return EXIT_OK


def cmd_release(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        released = hub.release(args.resource, force=args.force)
        # And drop your own declaration. It outlives the lease by a day; the
        # work did not, and a standing hold with nobody behind it is exactly
        # the litter this is meant to prevent rather than create.
        #
        # Your own only — see `holds.clear_own_declaration` for why force does
        # not reach this far.
        cleared = holds.clear_own_declaration(hub, args.resource)
    if args.json:
        _print_json({"released": released, "declaration_cleared": cleared})
    elif not args.quiet:
        print(f"released {args.resource}" if released else f"no lease on {args.resource}")
    return EXIT_OK


def cmd_claims(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        leases = hub.leases(holder=args.holder)
    if args.json:
        _print_json(leases)
        return EXIT_OK
    if not leases:
        print("nothing claimed")
        return EXIT_OK
    fmt = Fmt(_use_color(sys.stdout))
    # Say which column is blinded. A lease resource is an identifier, so under
    # a workspace key it leaves as an opaque token and comes back as one — the
    # hub compares it, it never reads it. Nothing said so, and an agent
    # verifying its own write lock grepped this output for the resource string
    # it had just passed, found nothing, and reported the lock as not held
    # while it was plainly listed in the same output. Same trap as the agent_id
    # blinding one surface over, and the note column was the only tell.
    blinded = bool(_make_config(args).key) and not args.json
    print(fmt.bold(f"{'RESOURCE':<38} {'HOLDER':<30} {'EXPIRES':<8} NOTE"))
    for le in leases:
        print(
            f"{le['resource'][:37]:<38} {le['holder'][:29]:<30} "
            f"{_dur(le['expires_in']):<8} {le.get('note') or ''}"
        )
    if blinded:
        print(fmt.dim(
            "\nRESOURCE and HOLDER are blinded: this workspace is encrypted, so "
            "identifiers\nleave as opaque tokens. They will not match the strings "
            "you passed — compare\nthe NOTE, or run `switchboard claims --json` "
            "and match on your own records."
        ), file=sys.stderr)
    _warn_unreadable(leases, "lease note(s)", fmt)
    return EXIT_OK


def cmd_say(args: argparse.Namespace) -> int:
    body = _read_body(args)
    # Posting is the event a speak forecast predicts, so it closes that
    # window before the next declaration opens a fresh one.
    timing = _Timing(args)
    timing.note_speak()
    forecast = timing.declare()
    timing.close()
    with _make_client(args) as hub:
        msg = hub.post(args.channel, wrap_forecast(body, forecast),
                       type=args.type, thread=args.thread, ttl=args.ttl)
        # Read inside the block: the hub attached it to the response we just
        # got, and the client is closed by the time we print.
        unread = hub.unread_dms
    if args.json:
        _print_json({**msg, "unread_dms": unread,
                     **({"timing_forecast": sender_forecast(forecast)} if forecast else {})})
    elif not args.quiet:
        print(f"posted #{msg['seq']} to {msg['channel']}")
        _print_unread(unread)
        if forecast:
            print(_forecast_line(Fmt(_use_color(sys.stdout)),
                                 forecast.as_message_meta(), "you expect to be"))
    return EXIT_OK


def _resolve_recipient(hub: Client, to: str, fmt: Fmt) -> str:
    """Turn what a human typed into something a peer's inbox actually reads.

    Two failures, both of which used to be silent, and both measured in this
    project's own dogfooding on 2026-08-17:

    An **operator-side label is not an address.** Two agents were told to call
    each other `switchboard_multi` and `roadmap_sep_agent`; only one pinned its
    id, so a DM to the other went to `blind("roadmap_sep_agent")` — a channel
    nobody was listening on. The hub accepted it and reported `sent #344`.

    And **an agent's own id is not stable across processes** (see
    `session_suffix`), while its *branch* is. So a branch or a name from the
    roster resolves here to the id that peer is currently reading, which is the
    same "match on branch, not id" rule the roadmap's claim reaper adopted for
    the same reason.

    Nothing here is an error. A peer legitimately between turns is absent from
    the roster and must still be reachable — `blind` is deterministic, so the
    message waits in the right place for them. What is not acceptable is doing
    that *silently*, because it is indistinguishable from a typo.
    """
    roster = hub.agents()
    by_id = {a.get("agent_id"): a for a in roster if a.get("agent_id")}
    if to in by_id:
        return to

    for field in ("branch", "name"):
        matches = [a for a in roster if a.get(field) == to and a.get("agent_id")]
        if len(matches) == 1:
            resolved = matches[0]["agent_id"]
            print(f"note: {to!r} matched the {field} of {resolved[:22]} on the roster; "
                  f"sending there.", file=sys.stderr)
            return resolved
        if len(matches) > 1:
            raise SystemExit(
                f"{to!r} matches the {field} of {len(matches)} agents on the roster. "
                f"Pass an agent_id from `switchboard agents` instead — a branch with "
                f"two live sessions is exactly when the wrong one would be picked."
            )

    if hub.peer_id(to) not in by_id:
        print(
            f"{fmt.yellow('warning')}: nobody on the roster answers to {to!r}. The "
            f"message will be accepted by the hub and read by nobody unless a peer "
            f"is between turns under exactly that id.\n"
            f"  ids come from `switchboard agents`, and are NOT the labels an "
            f"operator gave you — an unpinned agent derives its own.\n"
            f"  a peer that pinned SWITCHBOARD_AGENT_ID={to} will still receive this.",
            file=sys.stderr,
        )
    return to


def cmd_dm(args: argparse.Namespace) -> int:
    body = _read_body(args)
    timing = _Timing(args)
    timing.note_speak()
    forecast = timing.declare()
    timing.close()
    with _make_client(args) as hub:
        target = _resolve_recipient(hub, args.to, Fmt(_use_color(sys.stdout)))
        msg = hub.send(target, wrap_forecast(body, forecast),
                       type=args.type, thread=args.thread, ttl=args.ttl)
    if args.json:
        _print_json({**msg, **({"timing_forecast": sender_forecast(forecast)}
                               if forecast else {})})
    elif not args.quiet:
        print(f"sent #{msg['seq']} to {args.to}")
        if forecast:
            print(_forecast_line(Fmt(_use_color(sys.stdout)),
                                 forecast.as_message_meta(), "you expect to be"))
    return EXIT_OK


def cmd_whisper(args: argparse.Namespace) -> int:
    """`dm`'s sealed sibling: readable by `args.to` alone, not by anyone else
    who holds this workspace's key. See `docs/encryption.md` for what that
    costs — the recipient must already have been seen on the roster."""
    body = _read_body(args)
    timing = _Timing(args)
    timing.note_speak()
    forecast = timing.declare()
    timing.close()
    with _make_client(args) as hub:
        target = _resolve_recipient(hub, args.to, Fmt(_use_color(sys.stdout)))
        msg = hub.whisper(target, wrap_forecast(body, forecast), type=args.type, ttl=args.ttl)
        unread = hub.unread_dms
    if args.json:
        _print_json({**msg, "unread_dms": unread,
                     **({"timing_forecast": sender_forecast(forecast)} if forecast else {})})
    elif not args.quiet:
        print(f"whispered #{msg['seq']} to {args.to} (sealed to them alone)")
        _print_unread(unread)
        if forecast:
            print(_forecast_line(Fmt(_use_color(sys.stdout)),
                                 forecast.as_message_meta(), "you expect to be"))
    return EXIT_OK


def _print_unread(count: int) -> None:
    """Say that something is waiting, and only when something is.

    The guidance this CLI prints tells agents to watch `unread_dms` on every
    result; until this existed, the CLI was the one surface that never showed
    it, so an agent could be whispered at and never find out. Silent at zero on
    purpose — a line that appears every time is a line that stops being read.
    """
    if count > 0:
        noun = "message" if count == 1 else "messages"
        print(f"{count} unread direct {noun} waiting — read with `switchboard inbox`")


def _read_body(args: argparse.Namespace) -> Any:
    parts: Sequence[str] = args.message or []
    text = " ".join(parts)
    if text == "-" or (not text and not sys.stdin.isatty()):
        text = sys.stdin.read().strip()
    if args.json_body:
        try:
            return json.loads(text)
        except ValueError as exc:
            raise SystemExit(f"--json-body given but message is not valid JSON: {exc}") from exc
    return text


def _unopenable_note(message: dict[str, Any], *, peek: bool) -> str | None:
    """What to say about a message that arrived sealed and would not open.

    **Said at the moment it is lost, because that is the only useful moment.**
    A whisper you cannot open looks like noise, so the reasonable next move is
    to run `inbox` again and look closer — which advances the cursor past it
    and destroys it. Reported by an agent that did exactly that and never
    learned why its move had been refused: it was punished for being careful.

    `--peek` has always existed. Nothing told anyone at the point they needed
    it, which is the whole defect — the capability was there and the sentence
    was not. Nothing is said on a peek, because then nothing was consumed.
    """
    if not message.get("unreadable") or peek:
        return None
    return ("  ^ could not be opened: sealing is pairwise, so call `agents` to "
            "read the sender's exchange key, then ask them to resend. This "
            "message HAS been marked read — use `--peek` to look without "
            "consuming.")


def _learn_senders(hub: Any) -> None:
    """Read the roster before draining, so a sealed whisper can be opened.

    **Without this the CLI can never open a pairwise whisper, and destroys it
    trying.** Opening one needs the sender's `exchange_key`, and the only thing
    that puts it in `Client._peer_exchange_keys` is a roster call. The MCP
    server gets away with never doing it explicitly because it holds one
    long-lived client, so any earlier `roster` call has already filled the
    cache. Every CLI command is a fresh process, so the cache is always empty
    and every whisper arrives unopenable -- and `inbox` marks it read on the
    way past.

    Found the hard way in island game `g5`, where it hit BOTH traders at once.
    Each was sealed their private capacities and tastes, neither could open a
    single one, and no resend came; they played all eight episodes blind to
    their own preferences and settled two trades out of ten. One of them
    diagnosed it precisely and rebuilt the reader in Python as
    `register -> agents() -> inbox()`, which is exactly this function.

    `#172` added a sentence at the moment of loss, which was right and is not
    enough: it tells you what was destroyed after it is gone. This stops the
    destruction.

    Never raises. A roster that cannot be read is a worse inbox, not a failed
    one, and the note from `#172` still fires on anything that stays sealed.
    """
    try:
        hub.agents()
    except Exception:      # noqa: BLE001 -- see the docstring
        pass


def cmd_inbox(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        # Before the drain, never after: `inbox` consumes.
        if getattr(hub, "encrypted", False):
            _learn_senders(hub)
        messages = hub.inbox(
            channels=args.channel,
            wait=args.wait,
            limit=args.limit,
            peek=args.peek,
            include_own=args.include_own,
        )
    # Reading is the event every forecast predicts, so it closes the open
    # window whether or not the cursor moved and whether or not anything
    # arrived: a peek is still a look, and an empty drain is still a look.
    timing = _Timing(args)
    timing.note_look()
    forecast = timing.declare()
    timing.close()
    if args.json:
        _print_json({
            "messages": messages,
            **({"timing_forecast": sender_forecast(forecast)} if forecast else {}),
        })
        return EXIT_OK
    fmt = Fmt(_use_color(sys.stdout))
    if not messages:
        if not args.quiet:
            print("(nothing new)")
    for m in messages:
        head = f"{fmt.cyan(m['channel'])} {fmt.bold(m['from'])} {fmt.dim(_ago(m['created_at']))}"
        if m.get("type") and m["type"] != "note":
            head += f" {fmt.dim('[' + m['type'] + ']')}"
        print(head)
        print(f"  {_body_text(m['body'])}")
        note = _unopenable_note(m, peek=args.peek)
        if note:
            print(fmt.dim(note))
        _, incoming = unwrap_forecast(m["body"])
        if incoming:
            print(_forecast_line(fmt, incoming, "they expect to be"))
    if forecast and not args.quiet:
        print(_forecast_line(fmt, forecast.as_message_meta(), "you expect to be"))
    return EXIT_OK


# --- listen -------------------------------------------------------------
#
# The wake-on-message listener, as a command rather than a file.
#
# It shipped first as a shell script `init` wrote into each repo, which was the
# right shape for a hook and the wrong one for this: the generated wrapper
# bakes in the hub and workspace, so the listener was the only thing here that
# could not be pointed at another room. `-w`, `--invite` and the environment
# work on every other command; an agent coordinating across repos had to
# hand-assemble a script to get what `--invite` gives away for free.
#
# The mechanism never needed a file. A wake is a *process that exits*, and this
# process exits exactly as well as that one did.

#: How long a heartbeat lives without renewal. Three passes' slack, so one slow
#: long-poll cannot stage a death.
_LISTEN_TTL = 90.0

#: What the heartbeat says about its own absence. The viewer renders every
#: board key alike, and "expired" reads as housekeeping on all of them — true
#: for a stale handoff, badly wrong for this one.
_LISTEN_MEANS = (
    "this agent is parked and reachable; if this key is gone, nobody is "
    "listening and nothing will revive it"
)

#: 0 woken, 1 never watched anything, 2 reached the deadline. Distinct because
#: a woken agent has to tell them apart to know what to do next.
EXIT_DEADLINE = 2


def _listen_deadline(args: argparse.Namespace) -> tuple[float | None, str]:
    """Resolve `--until` to one absolute time, once.

    Once, not per pass: a forecast is "when will I next look, counting from
    now", so re-asking each time would push the deadline away every 25 seconds
    and the listener would never arrive at it.
    """
    raw: str | None = args.until
    if not raw:
        return None, ""
    if raw.startswith("forecast:"):
        quantile = raw.split(":", 1)[1]
        if quantile not in ("p50", "p95"):
            raise SystemExit(
                f"--until forecast:{quantile} — the timing model publishes p50 "
                "and p95, and nothing in between"
            )
        timing = _Timing(args)
        model = timing.model
        if model is None:
            raise SystemExit("--until forecast:… needs the local timing model, "
                             "and this machine has no readable store")
        try:
            # A preview, not a declaration: asking what the model would say
            # must not be recorded as having said it.
            forecast = model.forecast(
                timing.agent_id, timing.workspace,
                args.execution_class, args.effort)
        finally:
            timing.close()
        seconds = forecast.p50_seconds if quantile == "p50" else forecast.p95_seconds
        # `source` and `samples` decide what the number is worth: learned from
        # this machine's own history, or the deliberately wide bootstrap prior
        # a fresh container starts on. A deadline built on the prior should be
        # a shorter one, and the agent can only know that if we say so.
        return time.time() + seconds, (
            f"{quantile} {forecast.source}, {forecast.samples} sample(s)")
    if raw.startswith("+"):
        try:
            return time.time() + float(raw[1:]), raw
        except ValueError:
            raise SystemExit(f"--until {raw} is not a number of seconds") from None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(
            f"--until {raw} is not an ISO-8601 time, +SECONDS, or "
            "forecast:p50 / forecast:p95"
        ) from None
    if when.tzinfo is None:
        when = when.astimezone()
    return when.timestamp(), "given"


def cmd_listen(args: argparse.Namespace) -> int:
    """Park on the inbox until something arrives, then exit so the runner wakes
    this session.

    A coding session only reaches for its inbox when it is its turn, so a
    message that lands after the turn ends is delivered to nobody until a human
    types something. Runners that re-invoke a session when a background process
    exits — Claude Code does — make process death an available wake signal, and
    this turns "a message arrived" into "a process exited".
    """
    deadline, source = _listen_deadline(args)
    described = (
        datetime.fromtimestamp(deadline, timezone.utc).isoformat() if deadline else ""
    )
    if deadline and deadline <= time.time():
        print(f"listen: {described} ({source}) is already past", file=sys.stderr)
        return EXIT_DEADLINE

    identity = detect_identity(agent_id=args.agent_id)
    heartbeat_ttl = args.ttl or _LISTEN_TTL
    fails = 0
    passes = 0

    with _make_client(args) as hub:
        # The room, before the wait. A listener on the wrong hub or in the
        # wrong room does not fail — it waits, quietly, for as long as it is
        # allowed to, which is the failure this whole design exists to avoid.
        try:
            hub.health()
        except (SwitchboardError, OSError, httpx.HTTPError) as exc:
            # Including httpx's own errors, so a slow moment at arming time
            # produces this sentence rather than the generic handler's — and,
            # more to the point, so it is reported by the thing that knows it
            # was trying to park.
            print(f"listen: cannot reach {hub.config.url} ({exc})", file=sys.stderr)
            return EXIT_ERROR
        _warn_key_left_behind(args, hub)

        agent_id = hub.agent_id
        heartbeat = rendezvous.listener_key(agent_id)
        if not args.quiet:
            print(f"listen: parked as {agent_id} in {hub.config.workspace}"
                  + (f" until {described} [{source}]" if deadline else ""),
                  file=sys.stderr)
        try:
            while True:
                passes += 1
                wait = 25.0
                if deadline:
                    left = deadline - time.time()
                    # Never poll past the deadline: an unclamped last wait
                    # overshoots by up to 25 seconds, which is the whole margin
                    # on a short do-not-disturb and turns a time the agent
                    # named into one it approximated.
                    wait = min(wait, left)
                    if wait <= 0:
                        if not args.quiet:
                            print(f"listen: reached {described} [{source}] with "
                                  "nothing to report", file=sys.stderr)
                        return EXIT_DEADLINE
                try:
                    # Presence as well as the board key. A parked agent is the
                    # most reachable an agent ever is, and without this the
                    # roster says it does not exist: `agents` comes back empty
                    # and `dm` warns the sender their message will be read by
                    # nobody. `back_in` is the deadline, so an empty roster can
                    # be told from one that is merely between turns.
                    hub.register(
                        name=identity.name, kind=identity.kind,
                        branch=identity.branch,
                        # Deliberately no task. Presence is this process's
                        # business; what the agent is working on is the agent's,
                        # and a listener that rewrites it every pass turns the
                        # roster into a fight between two writers under one id.
                        # `back_in` below already renders as "away 12m", which
                        # is the fact a peer actually wants.
                        # None, not []: without a `-c` this process has
                        # nothing to say about subscriptions, and saying []
                        # every pass was how it removed them.
                        channels=args.channel or None,
                        meta=identity.meta, ttl=heartbeat_ttl,
                        back_in=(deadline - time.time()) if deadline else None,
                    )
                    hub.board_set(heartbeat, {
                        "pass": passes, "pid": os.getpid(), "waiting_on": "inbox",
                        **({"until": described, "until_source": source} if deadline else {}),
                        "means": _LISTEN_MEANS,
                    }, ttl=heartbeat_ttl)
                    # peek: this process shares a read cursor with the session
                    # it serves, so draining would consume the very message it
                    # woke that session to read. The woken session drains.
                    messages = hub.inbox(
                        channels=args.channel or None, wait=wait, peek=True)
                except (SwitchboardError, OSError, httpx.HTTPError) as exc:
                    # httpx.HTTPError belongs here as much as the other two,
                    # and its absence killed a listener in production: one
                    # ReadTimeout on a 25-second long-poll escaped this handler
                    # entirely, reached `main`, and exited 1 — "it never watched
                    # anything" — without `--max-fails` getting a say. A
                    # transient timeout is the most ordinary thing that happens
                    # to a process whose whole job is holding a connection open.
                    fails += 1
                    print(f"listen: hub error ({fails}/{args.max_fails}): {exc}",
                          file=sys.stderr)
                    if fails >= args.max_fails:
                        # A non-zero exit is still a wake, and that is the
                        # point: the session finds out its listener is gone
                        # rather than mistaking a broken hub for a quiet room.
                        print(f"listen: giving up after {fails} consecutive failures",
                              file=sys.stderr)
                        return EXIT_ERROR
                    time.sleep(min(fails * 2, 10))
                    continue
                fails = 0
                if messages:
                    # To stdout, because the wake carries it: the session comes
                    # back already holding the event instead of having to ask.
                    _print_json({"messages": messages})
                    if not args.quiet:
                        print(f"listen: message arrived on pass {passes} — "
                              "waking the session", file=sys.stderr)
                    return EXIT_OK
        finally:
            # Best effort, and only best effort: the TTL is what makes a dead
            # listener visible. This makes a deliberate stop visible sooner.
            try:
                hub.board_delete(heartbeat)
            except (SwitchboardError, OSError):
                pass


def _warn_key_left_behind(args: argparse.Namespace, hub: Client) -> None:
    """Say so when this invocation will watch a room it cannot read.

    `switchboard` does not read the workspace key out of
    `.claude/settings.local.json`, because a plain shell is not the MCP
    subprocess that file's env reaches. In an encrypted room a listener without
    the key watches a room it cannot address and finds nothing, ever — the same
    silence as a quiet room, for as long as anyone cares to wait.
    """
    if getattr(hub, "encrypted", False):
        return
    if _saved_key(Path(".").resolve()):
        print(
            "listen: this repo has a workspace key and this invocation does "
            "not have it.\n"
            "  You would watch a different room than the session you serve and "
            "never see a thing.\n"
            "  Pass --key, or export it:  export $(switchboard whoami --env | xargs)",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_ERROR)


def cmd_timing(args: argparse.Namespace) -> int:
    """Show this agent's own timing model — the local diagnostics the MCP
    bridge surfaces on its tool calls, which had no CLI surface at all.

    Everything here is local to this machine and never leaves it.
    """
    timing = _Timing(args)
    model = timing.model
    if model is None:
        print("no local timing store", file=sys.stderr)
        return EXIT_ERROR
    try:
        report = model.calibration(timing.agent_id, timing.workspace)
        classes = model.top_classes(timing.agent_id, timing.workspace)
        preview = None
        if args.execution_class or args.effort:
            # A forecast for the given labels *without* opening a window:
            # asking what the model would say must not be recorded as a
            # declaration, or every question would corrupt the history it
            # is asking about.
            preview = model.forecast(
                timing.agent_id, timing.workspace,
                args.execution_class, args.effort)
    finally:
        timing.close()

    if args.json:
        _print_json({
            "agent_id": timing.agent_id, "workspace": timing.workspace,
            "calibration": report, "classes": classes,
            **({"forecast": {**sender_forecast(preview),
                             "source": preview.source,
                             "samples": preview.samples}} if preview else {}),
        })
        return EXIT_OK

    fmt = Fmt(_use_color(sys.stdout))
    print(f"{fmt.dim('agent')}      {timing.agent_id}")
    print(f"{fmt.dim('classes')}    {', '.join(classes) or '(none yet)'}")
    if report["samples"] < MIN_SAMPLES:
        # Below MIN_SAMPLES the rates are noise, and a noisy number shown
        # without that caveat is worse than no number.
        print(f"{fmt.dim('samples')}    {report['samples']} "
              f"(too few to judge calibration; {MIN_SAMPLES} needed)")
    else:
        print(f"{fmt.dim('samples')}    {report['samples']}")
        print(f"{fmt.dim('p50 hits')}   {report['p50_hit_rate']:.0%} "
              f"{fmt.dim('(target ~50%)')}")
        print(f"{fmt.dim('p95 hits')}   {report['p95_hit_rate']:.0%} "
              f"{fmt.dim('(target ~95%)')}")
    if report["dropped_as_outliers"]:
        print(f"{fmt.dim('dropped')}    {report['dropped_as_outliers']} "
              f"{fmt.dim('too long to learn from — p95 above reads optimistic')}")
    if report.get("discarded_from_other_runs"):
        # The one number that explains a sample count stuck at zero.
        print(f"{fmt.dim('discarded')}  {report['discarded_from_other_runs']} "
              f"{fmt.dim('closed by a different run — never learned from')}")
        hint = "set SWITCHBOARD_RUNTIME_ID once per session if this keeps rising"
        print(f"           {fmt.dim(hint)}")
    if preview:
        print(f"{fmt.dim('forecast')}   p50 {_dur(preview.p50_seconds)}, "
              f"p95 {_dur(preview.p95_seconds)} "
              f"{fmt.dim('from ' + preview.source)}")
    return EXIT_OK


def cmd_history(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        messages = hub.history(args.channel, limit=args.limit)
    if args.json:
        _print_json(messages)
        return EXIT_OK
    fmt = Fmt(_use_color(sys.stdout))
    for m in messages:
        print(f"{fmt.dim(_ago(m['created_at']))} {fmt.bold(m['from'])}: {_body_text(m['body'])}")
    return EXIT_OK


def cmd_channels(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        channels = hub.channels()
    if args.json:
        _print_json(channels)
        return EXIT_OK
    for c in channels:
        print(f"{c['channel']:<30} {c['messages']:>4} msg  {_ago_epoch(c.get('latest_at'))}")
    return EXIT_OK


def _ago_epoch(ts: float | None) -> str:
    if not ts:
        return "-"
    delta = max(0, time.time() - ts)
    return _dur(delta) + " ago"


def cmd_board(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        if args.board_action == "set":
            # `-` reads the value from stdin, as `say` and `dm` already do.
            # Board values are the largest thing anyone puts through this CLI —
            # a handoff payload is the whole point of the blackboard — and
            # passing one as an argument means a shell gets to interpret it
            # first. A backtick in a message body was silently substituted away
            # mid-sentence during this project's own dogfooding; the same
            # character in a JSON payload takes the structure with it.
            raw: str = args.value
            if raw == "-":
                raw = sys.stdin.read().strip()
            value: Any = raw
            if args.json_body:
                try:
                    value = json.loads(raw)
                except ValueError as exc:
                    raise SystemExit(
                        f"--json-body given but the value is not valid JSON: {exc}"
                    ) from exc
            entry = hub.board_set(
                args.board_key, value, ttl=args.ttl, if_revision=args.if_revision
            )
            if args.json:
                _print_json(entry)
            elif not args.quiet:
                print(f"{entry['key']} = rev {entry['revision']}")
        elif args.board_action == "get":
            entry = hub.board_entry(args.board_key)
            if entry is None:
                print(f"no entry at {args.board_key}", file=sys.stderr)
                return EXIT_ERROR
            if args.json:
                _print_json(entry)
            else:
                print(_body_text(entry["value"]))
        elif args.board_action == "list":
            entries = hub.board_list(prefix=args.prefix)
            if args.json:
                _print_json(entries)
            else:
                for e in entries:
                    print(
                        f"{e['key']:<34} rev {e['revision']:<4} "
                        f"{e['updated_by'][:24]:<26} {_dur(e['expires_in'])}"
                    )
                _warn_unreadable(entries, "entry/entries", Fmt(_use_color(sys.stdout)))
        elif args.board_action == "delete":
            deleted = hub.board_delete(args.board_key)
            if not args.quiet:
                print("deleted" if deleted else "not found")
    return EXIT_OK


def cmd_checkin(args: argparse.Namespace) -> int:
    """Heartbeat, renew leases, and drain the inbox in one round-trip.

    Registers on demand, which the MCP bridge does for free and the CLI did
    not. Presence lasts DEFAULT_AGENT_TTL (120s), so a CLI agent hits this
    two ways: it never called `announce` at all, or it went quiet for longer
    than the TTL between calls. Both surface as the same 404, and both used
    to end the same way — an error telling the agent to go call something
    else, on the one command the coordination protocol says to call on a
    timer. The bridge recovers from exactly this in `_touch`/`checkin`; a
    heartbeat is a claim to be present, so making it true is more useful
    than reporting that it wasn't.
    """
    identity = detect_identity(agent_id=args.agent_id)

    def _register(hub: Client) -> None:
        hub.register(
            name=identity.name, kind=identity.kind, branch=identity.branch,
            task=args.task, meta=identity.meta, ttl=args.ttl,
        )

    with _make_client(args) as hub:
        try:
            result = hub.heartbeat(task=args.task, ttl=args.ttl,
                                   back_in=getattr(args, 'back_in', None))
        except SwitchboardError as exc:
            if exc.status != 404:
                raise
            _register(hub)
            result = hub.heartbeat(task=args.task, ttl=args.ttl,
                                   back_in=getattr(args, 'back_in', None))
        messages = hub.inbox(wait=args.wait, limit=args.limit)
    timing = _Timing(args)
    timing.note_look()
    forecast = timing.declare()
    timing.close()
    payload = {
        "agent": result["agent"],
        "leases": result["leases"],
        "messages": messages,
    }
    if args.json:
        if forecast:
            payload["timing_forecast"] = sender_forecast(forecast)
        _print_json(payload)
        return EXIT_OK
    fmt = Fmt(_use_color(sys.stdout))
    held = ", ".join(le["resource"] for le in result["leases"]) or "nothing"
    print(f"{fmt.dim('holding')}  {held}")
    if messages:
        print(f"{fmt.dim('new')}      {len(messages)} message(s)")
        for m in messages:
            print(f"  {fmt.cyan(m['channel'])} {fmt.bold(m['from'])}: {_body_text(m['body'])}")
            _, incoming = unwrap_forecast(m["body"])
            if incoming:
                print(_forecast_line(fmt, incoming, "they expect to be"))
    if forecast:
        print(_forecast_line(fmt, forecast.as_message_meta(), "you expect to be"))
    return EXIT_OK


def cmd_drill(args: argparse.Namespace) -> int:
    """Launch a few agents at one task and report what the hub saw.

    The worker kind is resolved rather than defaulted: `auto` means a real
    `claude` session when that binary exists and the scripted builtin
    otherwise, so the command does something useful on a laptop and in CI
    without either having to know which it is. Asking for `claude`
    explicitly and not having it is an error — silently downgrading a run
    the operator asked for to the fake one would make the report a lie.
    """
    if args.worker_kind == "auto":
        kind = "claude" if drill.claude_available() else "builtin"
    else:
        kind = args.worker_kind
    if kind in ("claude", "claude-mcp") and not drill.claude_available():
        print("no `claude` on PATH — use --worker builtin, or install Claude Code",
              file=sys.stderr)
        return EXIT_ERROR
    if kind == "claude-mcp" and not drill.mcp_server_available():
        # Its own check, and its own message: `claude` being present says
        # nothing about whether the MCP server this kind depends on can
        # start, and a worker whose server never starts has no hub tools at
        # all — which arrives as a silent run rather than as an error.
        print("no `switchboard-mcp` on PATH — install the package that provides it, "
              "or use --worker claude", file=sys.stderr)
        return EXIT_ERROR
    if kind == "custom" and not args.worker_cmd:
        print("--worker custom needs --worker-cmd", file=sys.stderr)
        return EXIT_ERROR

    fmt = Fmt(_use_color(sys.stdout))

    def note(text: str) -> None:
        if not args.quiet and not args.json:
            print(fmt.dim(text), file=sys.stderr)

    with _make_client(args) as hub:
        report = drill.run_drill(
            hub,
            task=args.task,
            count=args.agents,
            kind=kind,
            verify=args.verify,
            timeout=args.timeout,
            worker_cmd=args.worker_cmd,
            model=args.model,
            cwd=args.dir,
            on_event=note,
        )

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    if args.json:
        _print_json(report)
        return EXIT_OK if report["ok"] else EXIT_ERROR
    _print_drill_report(fmt, report)
    return EXIT_OK if report["ok"] else EXIT_ERROR


def _print_drill_report(fmt: Fmt, report: dict[str, Any]) -> None:
    counts = report["counts"]
    head = fmt.green("PASS") if report["ok"] else fmt.red("FAIL")
    print(f"{head}  drill {report['run_id']}  {_dur(report['wall_s'])}")
    print(fmt.dim(f"task    {report['task']}"))
    print(fmt.bold(f"\n{'WORKER':<8} {'VERDICT':<9} {'ANNOUNCE':<10} "
                   f"{'RESULT':<9} {'MSGS':<5} SUMMARY"))
    for worker in report["workers"]:
        verdict = worker["verdict"]
        paint = {"passed": fmt.green, "failed": fmt.red,
                 "silent": fmt.yellow, "no-report": fmt.yellow}[verdict]
        painted = paint(verdict)
        print(
            f"{worker['slot']:<8} {painted:<9} "
            f"{_secs(worker['announced_s']):<10} {_secs(worker['result_s']):<9} "
            f"{worker['messages']:<5} {worker['summary'] or '-'}"
        )
        for check in worker["checks"]:
            mark = fmt.green("ok") if check.get("ok") else fmt.red("!!")
            print(f"         {mark} {check.get('name', '?')}: {check.get('detail', '')}")
    telemetry = report["telemetry"]
    latency = telemetry["announce_latency_s"]
    tally = (f"\n{counts['workers']} worker(s): {counts['passed']} passed, "
             f"{counts['failed']} failed, {counts['silent']} silent")
    # Only when it happened. On the overwhelmingly common path this is zero,
    # and a column of zeroes is how a line stops being read.
    if counts.get("no_report"):
        tally += f", {counts['no_report']} no-report"
    print(fmt.dim(tally))
    if counts.get("no_report"):
        print(fmt.yellow(
            "no-report: the hub carried these workers' presence or messages, so "
            "coordination worked — look at the task, or at the result key they wrote to"
        ))
    print(fmt.dim(
        f"announce latency  min {_secs(latency['min'])}  mean {_secs(latency['mean'])}  "
        f"max {_secs(latency['max'])}   channel messages {telemetry['channel_messages']}  "
        f"hub polls {telemetry['hub_polls']}"
    ))
    if report["timed_out"]:
        print(fmt.yellow("run hit its timeout — remaining workers were terminated"))
    for err in telemetry["observer_errors"]:
        print(fmt.yellow(f"observer: {err}"), file=sys.stderr)
    print(fmt.dim(f"report kept on the board at {drill.report_key(report['run_id'])}"))


def _secs(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}s"


def cmd_watch(args: argparse.Namespace) -> int:
    """Follow messages continuously — a tail -f for the hub."""
    fmt = Fmt(_use_color(sys.stdout))
    with _make_client(args) as hub:
        timing = _Timing(args)
        try:
            while True:
                messages = hub.inbox(channels=args.channel, wait=25.0, limit=args.limit)
                # Same rule as `inbox`: the drain is the look, empty or not.
                timing.note_look()
                for m in messages:
                    print(
                        f"{fmt.dim(_ago(m['created_at']))} {fmt.cyan(m['channel'])} "
                        f"{fmt.bold(m['from'])}: {_body_text(m['body'])}",
                        flush=True,
                    )
        except KeyboardInterrupt:
            return EXIT_OK


def cmd_stats(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        _print_json(hub.stats())
    return EXIT_OK


def _handover(blob: invite.Invite, args: argparse.Namespace) -> tuple[str, str]:
    """What to print, and the one extra sentence a link needs.

    A link is for a human with a browser and no checkout — the reader who was
    never going to run `join`. It costs one warning, because the page in it is
    a page that will hold the key: the fragment keeps the key away from that
    host's *server*, and nothing keeps it away from the script it serves.

    Which is why the page a bare `--link` picks is the published one and not
    the viewer someone happens to have running. `PUBLISHED_PAGE_URL` is served
    from this repo with no build step, so a recipient can read the files the
    commit contains and know that is what ran in their browser. A page served
    off a laptop is the same script with none of that check — and, when the
    URL names a private address, not even a page: `http://192.168.1.7:8899`
    resolves on the *recipient's* network, where it is somebody else's router
    or nothing at all, while still opening perfectly for whoever sent it.
    """
    page = getattr(args, "link", None)
    if not page:
        return blob.encode(), ""
    host = urlsplit(page).netloc or page
    if is_private_page(page):
        return blob.link(page), (
            f"{host} is a private address. It resolves on whatever network "
            f"opens this\nlink, not on yours — so for anyone but you this "
            f"points at their own machine,\ntheir router, or nothing. Drop "
            f"the argument to link to the published page\n"
            f"({PUBLISHED_PAGE_URL}), which is served from this repo with no "
            f"build\nstep, so the recipient can diff what ran in their "
            f"browser against the commit."
        )
    return blob.link(page), (
        f"The key rides in the URL fragment, so {host} never receives it — "
        f"fragments\nare not sent to servers. But the page {host} serves is a "
        f"page that reads\nthe key. Link to one you control, or one you have "
        f"read and pinned."
    )


def _declared_room(workspace: str) -> rooms.Room | None:
    """The repo's record for the room being invited to, if it has one.

    Matched on the workspace rather than taken as "the selected room", and
    that is the whole care in this function. The room a repo *declares* and
    the room this invocation is actually in can differ — `-w`, an environment
    variable, a second entry in the file — and the two things this record
    supplies are both worse wrong than absent. A `key_id` naming the wrong key
    sends the receiver to a variable holding a real key that opens nothing
    here. A `workspace_token` from another room hashes to another identifier,
    and `Invite` refuses that outright.
    """
    try:
        for room in rooms.load(Path(".").resolve()):
            if room.workspace == workspace:
                return room
    except rooms.RoomsError:
        pass          # an unreadable rooms file is `rooms`' problem to report
    return None


def cmd_invite(args: argparse.Namespace) -> int:
    """Emit one string carrying everything a peer needs to join this room.

    Joining takes five facts and every one must match independently — and
    getting any wrong fails SILENTLY, which is what makes it expensive. Two
    agents in this project's own dogfooding spent forty minutes waiting for
    each other on the same hub and workspace with two different keys, both
    watching a roster that listed them both. One string is five chances to
    differ collapsed into one.
    """
    config = _make_config(args)
    key = args.key or config.key or _saved_key(Path(".").resolve())
    token = args.token or config.token or _saved_setting(
        Path(".").resolve(), "SWITCHBOARD_TOKEN"
    )
    # Seal a value the joiner can try to open. This is the only thing that
    # distinguishes "reached the same hub" from "in the same room": a joiner's
    # own round trip is self-consistent under any key, including the wrong one.
    probe = f"coord/join-probe/{secrets.token_urlsafe(8)}"
    try:
        with _make_client(args) as hub:
            hub.board_set(probe, invite.PROBE_SENTINEL, ttl=86400)
    except (SwitchboardError, OSError, CryptoError) as exc:
        print(f"error: could not leave a proof-of-room on the hub ({exc})",
              file=sys.stderr)
        return EXIT_ERROR
    declared = _declared_room(config.workspace)
    blob = invite.Invite(
        url=config.url, workspace=config.workspace,
        token=None if args.no_token else token,
        key=None if args.no_key else key,
        key_id=declared.key_id if declared else "",
        # Non-secret, and what lets the far side write this room down rather
        # than only visit it: a rooms file records rooms by token, because the
        # identifier is a hash and a hash does not run backwards.
        workspace_token=declared.workspace_token if declared else "",
        note=args.note or "", probe=probe,
    )
    handover, caveat = _handover(blob, args)
    if args.json:
        _print_json({"invite": blob.encode(), "describes": blob.redacted(),
                     **({"link": handover} if args.link else {})})
        return EXIT_OK
    print(handover)
    # Not inside the prompt block below: that one is gated on a TTY, and a
    # caveat about a credential in a URL is not a nicety for interactive
    # users. It is the reason the flag was worth thinking about.
    if caveat:
        print(f"\n{caveat}", file=sys.stderr)
    if _can_prompt(no_input=args.no_input, quiet=args.quiet, as_json=args.json):
        fmt = Fmt(_use_color(sys.stdout))
        print(
            f"\n{blob.redacted()}\n\n"
            + fmt.yellow("This is a credential.")
            + " It carries the token and the workspace key, so it\ngrants "
              "everything you have. Hand it over the way you would a password.\n\n"
            + ("The other side opens it in a browser." if args.link else
               "The other side runs:  switchboard join <the string above>\n"
               "  ...or, for one command in that room and no local change:\n"
               "                      switchboard --invite <the string> say build hi"),
            file=sys.stderr,
        )
        _offer_clipboard(handover, "the invite", args)
    return EXIT_OK


def cmd_join(args: argparse.Namespace) -> int:
    """Consume an invite, and prove the room rather than assuming it.

    Printing the settings is not the point — being in the same room is, and
    those are different claims. A roster listing both of you proves nothing:
    that is exactly what the forty-minute failure looked like. So this opens a
    value the *inviter* sealed, which only their key could have produced.

    The verdict comes from `Client.verify()` rather than from a comparison
    written out again here. Two of the failures are indistinguishable from one
    read — a wrong key blinds the probe's name to a token nobody stored, so the
    hub 404s exactly as it does for a probe that aged out — and telling them
    apart takes a second read this used to skip, answering "wrong key" to an
    expired probe and sending somebody off to re-key a room whose key was fine.
    One implementation, one wording, in the place the SDK already tests it.
    """
    fmt = Fmt(_use_color(sys.stdout))
    blob = getattr(args, "invite", None)
    if not blob:
        print("error: join needs the invite string — `switchboard join swb1_...`, "
              "the one\n`switchboard invite` printed on the other side.",
              file=sys.stderr)
        return EXIT_ERROR
    try:
        room = invite.Invite.decode(blob)
    except invite.InviteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(room.env_block())
    if room.workspace_token:
        # Exports get you in for this shell; a record keeps the room. Shown
        # rather than written, because writing a second room into a repo that
        # already declares one changes which room every *other* command in that
        # checkout lands in — see `switchboard rooms`. Whose repo this is, and
        # what to call the room, are not an invite's business.
        print(
            "\nTo keep this room rather than export it, add to "
            f"{rooms.ROOMS_LOCAL_FILE}:\n"
            + json.dumps({"rooms": [room.room_record(args.save or "invited")]},
                         indent=2)
            + (f"\n\nand set {rooms.env_var_for(room.key_id)} to the key above."
               if room.key_id else "")
        )

    if args.no_verify:
        if args.json:
            _print_json({
                "url": room.url, "workspace": room.workspace,
                "encrypted": bool(room.key), "note": room.note,
                "verdict": "unchecked",
            })
        return EXIT_OK

    # Verify by opening a sealed value through the hub. A peer on a different
    # key cannot produce one we can read, which is the only check that
    # distinguishes "same room" from "same roster".
    try:
        with Client.from_invite(
            room, agent_id=f"join-probe-{secrets.token_hex(4)}"
        ) as peer:
            check = peer.verify()
    except (SwitchboardError, OSError, CryptoError) as exc:
        if args.json:
            _print_json({
                "url": room.url, "workspace": room.workspace,
                "encrypted": bool(room.key), "note": room.note,
                "verdict": "unreachable", "detail": str(exc),
            })
        print(
            f"\n{fmt.red('could not reach that room')}: {exc}\n"
            "The settings above are what the invite said; something between "
            "here and the hub\ndisagrees. Nothing has been changed locally.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if args.json:
        _print_json({
            "url": room.url, "workspace": room.workspace,
            "encrypted": bool(room.key), "note": room.note,
            "verdict": check.verdict, "ok": check.ok, "detail": check.detail,
        })

    if check.verdict == "no_probe":
        print(
            f"\n{fmt.yellow('cannot verify')} — this invite carries no "
            "proof-of-room. The settings\nabove are what it said; nothing "
            "checked them.",
            file=sys.stderr,
        )
        return EXIT_OK

    if check.verdict == "probe_gone":
        # Not a failure to act on, and emphatically not a key problem: saying
        # so is the point. Everything here expires; the invite simply outlived
        # the proof it referred to.
        print(
            f"\n{fmt.yellow('cannot verify')} — the proof the inviter left is no "
            "longer on the\nblackboard. Everything here expires, so it has most "
            "likely aged out;\nnothing suggests your key is wrong. Ask for a fresh "
            "invite to have this\nchecked.",
            file=sys.stderr,
        )
        return EXIT_OK

    if not check.ok:
        # Reached the hub, could not open what the inviter left. Same hub, same
        # workspace, different key — two agents listed on one roster, able to
        # exchange nothing. Exactly the forty minutes this exists to save.
        print(
            f"\n{fmt.red('WRONG ROOM')} — reached that hub and workspace, but could "
            "not read\nthe proof the inviter left. Your key does not match theirs.\n\n"
            "You would have appeared on each other's roster and been unable to\n"
            "exchange anything. Ask for a fresh invite rather than editing settings.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    # The wording comes from the check rather than from here, because what
    # `verified` proves depends on whether there was a key: in a plaintext room
    # the probe is readable by anyone who can reach that hub and name that
    # workspace, and claiming a key matched when there was none to match is the
    # kind of overclaim that makes the whole check less worth trusting.
    print(f"\n{fmt.green('verified')} — {check.detail}."
          + (f"\n\n{room.note}" if room.note else ""), file=sys.stderr)
    return EXIT_OK


def cmd_health(args: argparse.Namespace) -> int:
    config = _make_config(args)
    try:
        with Client(config, agent_id=detect_identity(agent_id=args.agent_id).agent_id) as hub:
            payload = hub.health()
    except Exception as exc:  # noqa: BLE001 - health check reports any failure
        print(f"unreachable: {exc}", file=sys.stderr)
        return EXIT_ERROR
    # Note there is no `--json` branch here: this command's stdout is always
    # the hub's JSON. So the warning has nowhere to go but stderr, which is
    # where it belongs anyway — `{"ok": true}` about a hub nobody else can
    # reach is the most misleading of the three outputs, and the one most
    # likely to be piped into something that decides everything is fine.
    _print_json(payload)
    _warn_isolated(args, config)
    return EXIT_OK


# --- init ---------------------------------------------------------------
#
# Everything below wires up a repo so agents pick up a hub with zero manual
# copy-pasting from docs: an .mcp.json entry, lifecycle hooks, and the
# CLAUDE.md section that tells an agent when to use them. Every writer here
# merges into whatever is already on disk and is safe to run more than once.

_LOCAL_HUB_URLS = {"http://127.0.0.1:8787", "http://localhost:8787"}

# MANAGED_HUB_URL and MANAGED_HUB_TOKEN moved to config.py, and are imported
# above so `switchboard.cli.MANAGED_HUB_URL` still resolves. They describe
# where a *client* dials, and the MCP bridge builds its client straight from
# `ClientConfig.from_env` without ever importing this module — a constant it
# cannot see is a default it cannot honour, which is how `init` came to point
# at the managed hub while every un-inited client dialled localhost.


def _hook_env_prefix(url: str, workspace: str) -> str:
    """`SessionStart`/`Stop` hooks run as plain shell commands, not inside the
    `switchboard-mcp` subprocess — `.mcp.json`'s `env` block never reaches
    them, so `SWITCHBOARD_URL`/`SWITCHBOARD_WORKSPACE` may not be ambient
    there even when a token is (see #32). Neither is secret, and both are
    already known at `init` time, so export them explicitly rather than
    relying on an environment the hook doesn't actually share. `export`
    rather than a simple-command prefix so it also reaches the commands on
    the far side of `|` and the `subprocess.run` calls made by the nested
    `python -c` below — a plain `VAR=val cmd` prefix would not."""
    return (
        f"export SWITCHBOARD_URL={shlex.quote(url)}; "
        f"export SWITCHBOARD_WORKSPACE={shlex.quote(workspace)}; "
    )


#: Switchboard's own directory in a repo. What goes in here is ours: we wrote
#: it, we recognize it, and we may replace it. Nothing else in the repo is.
_SWITCHBOARD_DIR = ".switchboard"
_HOOKS_DIR = f"{_SWITCHBOARD_DIR}/hooks"

#: The body of each lifecycle hook, as a shell script *we own*.
#:
#: These used to be inlined into `.claude/settings.json` as single-line
#: commands, which made every upgrade a guess about whether the string in
#: someone else's config file was still ours — hence `_revision_status`, the
#: history lists, and a confirmation prompt, all of them machinery for
#: recognizing our own output in a file we do not control. Owning the body
#: outright removes the guess: this file has one author, and the only thing
#: left in the agent's config is a shim that never changes.
#:
#: It also stops the hooks being Claude-specific. A shell script is something
#: any runner can invoke — see docs/codex-cli.md — so adding a second agent is
#: a second registration, not a second implementation.
#: What a fresh machine needs before any of this works: the package. That is
#: the step people forget, and it fails in the worst way — `switchboard-mcp` is
#: simply not on PATH, so the MCP server never starts and the session has no
#: switchboard tools at all, with the secrets set correctly the whole time.
#:
#: So the committed config resolves the tool rather than assuming it. An
#: installed binary wins, because an environment that pinned a version meant
#: it. Otherwise `uvx` runs it from a cache without installing anything into
#: the environment, so a clone or a cloud session works with no setup step.
#:
#: Deliberately no `pip install` fallback: fetching into a cache is one thing,
#: mutating the environment from a file someone merely checked out is another.
#: If neither is available it says so and exits, which is a failure you can
#: read rather than a session that quietly has no tools.
_UVX_SPEC = "agent-switchboard[crypto]"

_NOT_FOUND_HINT = (
    "switchboard: %s not found and uvx unavailable; "
    "pip install '" + _UVX_SPEC + "'"
)


def _bootstrap_exec(binary: str) -> str:
    """POSIX sh that execs `binary`, fetching it through uvx if it is absent."""
    return (
        "command -v {b} >/dev/null 2>&1 && exec {b}; "
        "command -v uvx >/dev/null 2>&1 && exec uvx --from {spec} {b}; "
        "echo {hint} >&2; exit 127"
    ).format(
        b=binary,
        spec=shlex.quote(_UVX_SPEC),
        hint=shlex.quote(_NOT_FOUND_HINT % binary),
    )


def _bootstrap_fn(binary: str, fn: str) -> str:
    """The same resolution, as a shell function so a script can call it many
    times without re-resolving on every line."""
    return (
        "if command -v {b} >/dev/null 2>&1; then\n"
        "  {fn}() {{ {b} \"$@\"; }}\n"
        "elif command -v uvx >/dev/null 2>&1; then\n"
        "  {fn}() {{ uvx --from {spec} {b} \"$@\"; }}\n"
        "else\n"
        "  echo {hint} >&2\n"
        "  exit 0\n"
        "fi\n"
    ).format(
        b=binary,
        fn=fn,
        spec=shlex.quote(_UVX_SPEC),
        hint=shlex.quote(_NOT_FOUND_HINT % binary),
    )


#: The v1 bodies, which invoked `switchboard` directly. Kept so a repo written
#: by an earlier `init` is recognized as machine output and upgraded, rather
#: than read as a hand edit and left alone forever.
_SESSION_START_BODY_V1 = "switchboard -q register -c build"
_STOP_BODY_V1 = (
    'switchboard --json claims --holder "$(switchboard --json whoami | '
    "python -c 'import sys,json;print(json.load(sys.stdin)[\"agent_id\"])')\" | "
    "python -c 'import sys,json,subprocess;"
    "[subprocess.run([\"switchboard\",\"-q\",\"release\",l[\"resource\"]]) "
    "for l in json.load(sys.stdin)]'"
)

_SESSION_START_BODY = "sb -q register -c build"
#: Every switchboard call goes through the `sb` function the prefix defines, so
#: releasing happens in shell rather than from inside a python subprocess —
#: a function is not visible to a spawned process, and re-resolving the binary
#: per lease would be worse than reading it.
_STOP_BODY = (
    'holder="$(sb --json whoami | '
    "python -c 'import sys,json;print(json.load(sys.stdin)[\"agent_id\"])')\"\n"
    'sb --json claims --holder "$holder" | '
    "python -c 'import sys,json;"
    "[print(l[\"resource\"]) for l in json.load(sys.stdin)]' | "
    'while IFS= read -r resource; do sb -q release "$resource"; done'
)


def _hook_script(body: str, url: str, workspace: str, bootstrap: bool = True) -> str:
    """One lifecycle hook, as a standalone script.

    The URL and workspace are still baked in rather than read from the
    environment, for exactly the reason #32 exists: a hook runs as a plain
    shell command and does not share `.mcp.json`'s `env`, so a session can
    have a token ambient and nothing else. Neither value is secret and both
    are known at `init` time.
    """
    return (
        "#!/bin/sh\n"
        "# Generated by `switchboard init`. Safe to delete; it will come back\n"
        "# on the next run. If you edit it, `init` will leave it alone from\n"
        "# then on and tell you so.\n"
        # rstrip: the prefix is shaped for inlining ahead of a command on one
        # line, which leaves a stray separator when it ends a line instead.
        f"{_hook_env_prefix(url, workspace).rstrip()}\n"
        + (_bootstrap_fn("switchboard", "sb") if bootstrap
           else 'sb() { switchboard "$@"; }\n')
        + f"{body}\n"
    )


def _session_start_script(url: str, workspace: str, bootstrap: bool = True) -> str:
    return _hook_script(_SESSION_START_BODY, url, workspace, bootstrap)


def _stop_script(url: str, workspace: str, bootstrap: bool = True) -> str:
    return _hook_script(_STOP_BODY, url, workspace, bootstrap)


#: Where the listener lands: beside `hooks/`, not in it. See `_init_hook_scripts`.
_WAKE_REL = f"{_SWITCHBOARD_DIR}/{WAKE_SCRIPT_NAME}"


def _wake_script(url: str, workspace: str, bootstrap: bool = True) -> str:
    """The wake-on-message listener, wrapped the same way a hook is.

    It needs the wrapper for the same reason the hooks do and slightly more
    urgently: it runs as a plain background shell, so `.mcp.json`'s env never
    reaches it, and a listener pointed at the wrong hub or the wrong workspace
    does not fail — it waits, quietly, forever.
    """
    return _hook_script(wake_script_body(), url, workspace, bootstrap)


def _hook_shim(name: str) -> str:
    """What goes in the agent's config: one line, identical in every repo and
    every revision, so recognizing it later is an exact match rather than a
    heuristic.

    `CLAUDE_PROJECT_DIR` is Claude Code's, and belongs here — this string is
    what gets written into Claude Code's settings file. The script it calls
    stays runner-agnostic. Hooks are invoked from the project root, so the
    fallback covers a runner that sets nothing.
    """
    return f'sh "${{CLAUDE_PROJECT_DIR:-.}}/{_HOOKS_DIR}/{name}.sh" || true'


def _session_start_cmd(url: str, workspace: str) -> str:
    return _hook_shim("session-start")


def _stop_cmd(url: str, workspace: str) -> str:
    return _hook_shim("stop")


#: Every hook command `init` has ever generated, oldest first, excluding the
#: current one — extracted verbatim from git history, not retyped, so a repo
#: whose hooks are untouched output from a past `init` run (rather than
#: hand-edited) gets recognized and safely upgraded on the next run instead
#: of being left on a stale — possibly buggy, see #32 — revision forever.
_SESSION_START_CMD_HISTORY: list[Callable[[str, str], str]] = [
    lambda url, workspace: "switchboard register --quiet -c build || true",
    lambda url, workspace: "switchboard -q register -c build || true",
    # The last inline revision, before the body moved into a script we own.
    # Listed here so an already-initialized repo is recognized and migrated to
    # the shim on the next `init` rather than being read as a hand edit.
    lambda url, workspace: (
        f"{_hook_env_prefix(url, workspace)}switchboard -q register -c build || true"
    ),
]
_STOP_CMD_HISTORY: list[Callable[[str, str], str]] = [
    lambda url, workspace: (
        'switchboard claims --holder "$(switchboard whoami --json | '
        "python -c 'import sys,json;print(json.load(sys.stdin)[\"agent_id\"])')\" --json | "
        "python -c 'import sys,json,subprocess;"
        "[subprocess.run([\"switchboard\",\"release\",l[\"resource\"],\"--quiet\"]) "
        "for l in json.load(sys.stdin)]' "
        "|| true"
    ),
    lambda url, workspace: (
        'switchboard --json claims --holder "$(switchboard --json whoami | '
        "python -c 'import sys,json;print(json.load(sys.stdin)[\"agent_id\"])')\" | "
        "python -c 'import sys,json,subprocess;"
        "[subprocess.run([\"switchboard\",\"-q\",\"release\",l[\"resource\"]]) "
        "for l in json.load(sys.stdin)]' "
        "|| true"
    ),
    # The last inline revision — see the note in _SESSION_START_CMD_HISTORY.
    lambda url, workspace: (
        f"{_hook_env_prefix(url, workspace)}"
        'switchboard --json claims --holder "$(switchboard --json whoami | '
        "python -c 'import sys,json;print(json.load(sys.stdin)[\"agent_id\"])')\" | "
        "python -c 'import sys,json,subprocess;"
        "[subprocess.run([\"switchboard\",\"-q\",\"release\",l[\"resource\"]]) "
        "for l in json.load(sys.stdin)]' "
        "|| true"
    ),
]


def _revision_status(existing: str, current: str, history: Sequence[str]) -> str:
    """Classify existing generated content against the current canonical
    version and every past revision it might be untouched output from.

    "current" — nothing to do. "stale" — matches a past revision exactly, so
    it's machine output we can safely replace. "unrecognized" — matches
    nothing we ever generated, so it's probably hand-edited; leave it alone
    unless told to --force."""
    if existing == current:
        return "current"
    if existing in history:
        return "stale"
    return "unrecognized"


_CLAUDE_MD_MARKER = "## Coordinating with other agents"
_CLAUDE_MD_SECTION = f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **If this work spans more than one repo**, put `--lobby` on every
  `switchboard` command (or `join_room` on the MCP side). Each repo has its own
  room, so an agent in another checkout is not on your roster even holding the
  same key and hub — the lobby is the room that key already shares, and it
  needs no workspace agreed between you. Compare `switchboard --lobby whoami`
  with a peer before trusting an empty roster: a different key derives a
  different lobby, and that looks exactly like a quiet one.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
  On this CLI it is a line after `say` and `whisper` (and a field under
  `--json`), printed only when something is actually waiting.
- **If you are ending a turn while still waiting on another agent**, read
  `.claude/skills/switchboard-coordinate/SKILL.md` for how to schedule a
  check-in instead of leaving the wait unbounded — `unread_dms` only helps
  while you are still making tool calls, and nothing else will interrupt an
  idle session.
- **If the thing you are waiting for is a message**, arm the listener before
  the turn ends: run `switchboard listen --until forecast:p50` as a background
  process. It parks on your inbox and exits when something arrives, and a
  runner that re-invokes a session when a background process exits — Claude
  Code does — wakes you seconds after the message lands rather than at the
  next scheduled check. `--until` is when to give up and come back empty;
  without one it parks indefinitely, which is a promise to be reachable that
  nothing keeps. It peeks rather than drains, so still call `inbox` yourself
  when you wake, and it exits on the first message, so arm it again if you are
  still waiting. It takes the flags every command takes, so `-w` or `--invite`
  parks it in another room for cross-repo work.
- **Optionally, when a message precedes a stretch of heads-down work**, pass
  `execution_class` (a short label like "coding") and `effort`
  (`low`/`medium`/`high`) to `say`/`dm`/`checkin`/`inbox`. Your runtime turns
  that pair into an estimate of when you will next read messages and attaches
  it for collaborators — you never estimate seconds. Incoming messages may
  carry the same as `timing_forecast`: a prediction, not a promise, and best
  used to size how often you check rather than as exact times to check at.
- **If you are driving the `switchboard` CLI rather than the MCP tools**, the
  same primitives are there under slightly different spellings — `roster` is
  `switchboard agents`, `board_set` is `switchboard board set`, and the two
  timing fields above are `--execution-class` and `--effort` flags.
  `.claude/skills/switchboard-coordinate/SKILL.md` has the full mapping and
  the two things only the MCP surface offers.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message — messages are for signals, the blackboard is
  for payloads. `.claude/skills/switchboard-coordinate/SKILL.md` has the
  shared key-naming convention that keeps independent sessions finding each
  other's handoffs instead of missing them.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
"""


#: Every _CLAUDE_MD_SECTION `init` has ever generated, oldest first,
#: excluding the current one — extracted verbatim from git history, not
#: retyped, for the same reason as the hook-command history above: a repo
#: whose CLAUDE.md section is untouched output from a past `init` run
#: gets recognized and upgraded automatically instead of stuck forever.
_CLAUDE_MD_SECTION_HISTORY: list[str] = [
    f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive and hands you anything other agents have said. If you stop calling it,
  your claims expire and free themselves — which is correct if you have
  crashed and wrong if you are still working.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message. Messages are for signals; the blackboard is
  for payloads.

Switchboard is ephemeral by design. Anything that should outlive the work
still belongs in a commit message, a PR body, or a doc — not in a channel.
""",
    f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message. Messages are for signals; the blackboard is
  for payloads.

Switchboard is ephemeral by design. Anything that should outlive the work
still belongs in a commit message, a PR body, or a doc — not in a channel.
""",
    f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
  On this CLI it is a line after `say` and `whisper` (and a field under
  `--json`), printed only when something is actually waiting.
- **If you are ending a turn while still waiting on another agent**, and your
  environment can schedule a future wake-up, use it to check back rather than
  letting the wait go unbounded — a short interval if you are waiting on one
  specific reply, longer for a general "check in later." `unread_dms` only
  helps while you are still making tool calls; it does nothing once you have
  gone idle, and nothing else will interrupt you. When the wake-up fires,
  `checkin` tells you whether anything changed.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message. Messages are for signals; the blackboard is for
  payloads.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
""",
    f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
  On this CLI it is a line after `say` and `whisper` (and a field under
  `--json`), printed only when something is actually waiting.
- **If you are ending a turn while still waiting on another agent**, read
  `.claude/skills/switchboard-coordinate/SKILL.md` for how to schedule a
  check-in instead of leaving the wait unbounded — `unread_dms` only helps
  while you are still making tool calls, and nothing else will interrupt an
  idle session.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message — messages are for signals, the blackboard is
  for payloads. `.claude/skills/switchboard-coordinate/SKILL.md` has the
  shared key-naming convention that keeps independent sessions finding each
  other's handoffs instead of missing them.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
""",
    f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
  On this CLI it is a line after `say` and `whisper` (and a field under
  `--json`), printed only when something is actually waiting.
- **If you are ending a turn while still waiting on another agent**, read
  `.claude/skills/switchboard-coordinate/SKILL.md` for how to schedule a
  check-in instead of leaving the wait unbounded — `unread_dms` only helps
  while you are still making tool calls, and nothing else will interrupt an
  idle session.
- **Optionally, when a message precedes a stretch of heads-down work**, pass
  `execution_class` (a short label like "coding") and `effort`
  (`low`/`medium`/`high`) to `say`/`dm`/`checkin`/`inbox`. Your runtime turns
  that pair into an estimate of when you will next read messages and attaches
  it for collaborators — you never estimate seconds. Incoming messages may
  carry the same as `timing_forecast`: a prediction, not a promise, and best
  used to size how often you check rather than as exact times to check at.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message — messages are for signals, the blackboard is
  for payloads. `.claude/skills/switchboard-coordinate/SKILL.md` has the
  shared key-naming convention that keeps independent sessions finding each
  other's handoffs instead of missing them.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
""",
    f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
  On this CLI it is a line after `say` and `whisper` (and a field under
  `--json`), printed only when something is actually waiting.
- **If you are ending a turn while still waiting on another agent**, read
  `.claude/skills/switchboard-coordinate/SKILL.md` for how to schedule a
  check-in instead of leaving the wait unbounded — `unread_dms` only helps
  while you are still making tool calls, and nothing else will interrupt an
  idle session.
- **Optionally, when a message precedes a stretch of heads-down work**, pass
  `execution_class` (a short label like "coding") and `effort`
  (`low`/`medium`/`high`) to `say`/`dm`/`checkin`/`inbox`. Your runtime turns
  that pair into an estimate of when you will next read messages and attaches
  it for collaborators — you never estimate seconds. Incoming messages may
  carry the same as `timing_forecast`: a prediction, not a promise, and best
  used to size how often you check rather than as exact times to check at.
- **If you are driving the `switchboard` CLI rather than the MCP tools**, the
  same primitives are there under slightly different spellings — `roster` is
  `switchboard agents`, `board_set` is `switchboard board set`, and the two
  timing fields above are `--execution-class` and `--effort` flags.
  `.claude/skills/switchboard-coordinate/SKILL.md` has the full mapping and
  the two things only the MCP surface offers.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message — messages are for signals, the blackboard is
  for payloads. `.claude/skills/switchboard-coordinate/SKILL.md` has the
  shared key-naming convention that keeps independent sessions finding each
  other's handoffs instead of missing them.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
""",
    f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
  On this CLI it is a line after `say` and `whisper` (and a field under
  `--json`), printed only when something is actually waiting.
- **If you are ending a turn while still waiting on another agent**, read
  `.claude/skills/switchboard-coordinate/SKILL.md` for how to schedule a
  check-in instead of leaving the wait unbounded — `unread_dms` only helps
  while you are still making tool calls, and nothing else will interrupt an
  idle session.
- **If the thing you are waiting for is a message**, arm the listener before
  the turn ends: run `sh .switchboard/wake-on-message.sh` as a background
  process. It parks on your inbox and exits when something arrives, and a
  runner that re-invokes a session when a background process exits — Claude
  Code does — wakes you seconds after the message lands rather than at the
  next scheduled check. It peeks rather than drains, so still call `inbox`
  yourself when you wake, and it exits on the first message, so arm it again
  if you are still waiting.
- **Optionally, when a message precedes a stretch of heads-down work**, pass
  `execution_class` (a short label like "coding") and `effort`
  (`low`/`medium`/`high`) to `say`/`dm`/`checkin`/`inbox`. Your runtime turns
  that pair into an estimate of when you will next read messages and attaches
  it for collaborators — you never estimate seconds. Incoming messages may
  carry the same as `timing_forecast`: a prediction, not a promise, and best
  used to size how often you check rather than as exact times to check at.
- **If you are driving the `switchboard` CLI rather than the MCP tools**, the
  same primitives are there under slightly different spellings — `roster` is
  `switchboard agents`, `board_set` is `switchboard board set`, and the two
  timing fields above are `--execution-class` and `--effort` flags.
  `.claude/skills/switchboard-coordinate/SKILL.md` has the full mapping and
  the two things only the MCP surface offers.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message — messages are for signals, the blackboard is
  for payloads. `.claude/skills/switchboard-coordinate/SKILL.md` has the
  shared key-naming convention that keeps independent sessions finding each
  other's handoffs instead of missing them.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
""",
    f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
  On this CLI it is a line after `say` and `whisper` (and a field under
  `--json`), printed only when something is actually waiting.
- **If you are ending a turn while still waiting on another agent**, read
  `.claude/skills/switchboard-coordinate/SKILL.md` for how to schedule a
  check-in instead of leaving the wait unbounded — `unread_dms` only helps
  while you are still making tool calls, and nothing else will interrupt an
  idle session.
- **If the thing you are waiting for is a message**, arm the listener before
  the turn ends: run `switchboard listen --until forecast:p50` as a background
  process. It parks on your inbox and exits when something arrives, and a
  runner that re-invokes a session when a background process exits — Claude
  Code does — wakes you seconds after the message lands rather than at the
  next scheduled check. `--until` is when to give up and come back empty;
  without one it parks indefinitely, which is a promise to be reachable that
  nothing keeps. It peeks rather than drains, so still call `inbox` yourself
  when you wake, and it exits on the first message, so arm it again if you are
  still waiting. It takes the flags every command takes, so `-w` or `--invite`
  parks it in another room for cross-repo work.
- **Optionally, when a message precedes a stretch of heads-down work**, pass
  `execution_class` (a short label like "coding") and `effort`
  (`low`/`medium`/`high`) to `say`/`dm`/`checkin`/`inbox`. Your runtime turns
  that pair into an estimate of when you will next read messages and attaches
  it for collaborators — you never estimate seconds. Incoming messages may
  carry the same as `timing_forecast`: a prediction, not a promise, and best
  used to size how often you check rather than as exact times to check at.
- **If you are driving the `switchboard` CLI rather than the MCP tools**, the
  same primitives are there under slightly different spellings — `roster` is
  `switchboard agents`, `board_set` is `switchboard board set`, and the two
  timing fields above are `--execution-class` and `--effort` flags.
  `.claude/skills/switchboard-coordinate/SKILL.md` has the full mapping and
  the two things only the MCP surface offers.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message — messages are for signals, the blackboard is
  for payloads. `.claude/skills/switchboard-coordinate/SKILL.md` has the
  shared key-naming convention that keeps independent sessions finding each
  other's handoffs instead of missing them.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
""",
]


def _git_remote_workspace(directory: Path) -> str | None:
    """Guess `org/repo` from the git remote, or None if there isn't one.

    Delegates to `config.git_remote_workspace` rather than keeping a second
    copy. `init` and the runtime default used to derive this separately, and
    two derivations that can disagree are two rooms an unconfigured agent can
    land in — the same shape of failure as the two hub defaults that drifted.
    """
    return git_remote_workspace(directory)


def _default_workspace(directory: Path) -> str:
    """The workspace to use when nobody named one.

    A git remote is the good case: every clone derives the same name, which is
    what gets a laptop, a cloud session and a CI runner into the same room for
    free.

    Without one there is nothing shared to derive from, and the bare directory
    name is a poor substitute — `api` or `backend` collides with everyone else
    who has a directory called that, and on a shared hub they land on top of
    each other. A machine tag keeps it unique. It stays readable here on
    purpose: the user chose this directory name, and `--new-key` replaces the
    whole thing with an opaque one anyway when hiding it from the hub is the
    point.
    """
    resolved = directory.resolve()
    remote = _git_remote_workspace(directory)
    return remote or f"{resolved.name}-{machine_suffix(str(resolved))}"


def _init_token(directory: Path, token: str | None) -> tuple[str, str]:
    """Resolve a dev token for a local hub, generating and persisting one if needed."""
    if token:
        return token, "using the provided token"
    env_token = os.environ.get("SWITCHBOARD_TOKEN")
    if env_token:
        return env_token, "using SWITCHBOARD_TOKEN from your environment"
    env_path = directory / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("SWITCHBOARD_TOKEN="):
                return line.split("=", 1)[1].strip(), "reused the token already in .env"
    generated = secrets.token_urlsafe(32)
    with env_path.open("a") as f:
        f.write(f"SWITCHBOARD_TOKEN={generated}\n")
    return generated, "generated a dev token, saved to .env (already gitignored)"


def _mcp_entry(directory: Path) -> dict | None:
    """The switchboard MCP server entry already registered in this repo."""
    path = directory / ".mcp.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return None
    servers = data.get("mcpServers")
    entry = servers.get("switchboard") if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


_mcp_env = mcp_env


def _mcp_url(directory: Path) -> str | None:
    return mcp_env(directory, "SWITCHBOARD_URL")


def _mcp_workspace(directory: Path) -> str | None:
    """The workspace an already-registered switchboard MCP server routes to.

    This is what actually decides which room this repo's agents land in, so
    it is the value a key has to be paired with — not whatever `init` would
    have picked had the file not been there.
    """
    return mcp_env(directory, "SWITCHBOARD_WORKSPACE")


def _init_mcp_json(
    directory: Path, url: str, workspace: str, *, overwrite: bool = False,
    bootstrap: bool = True,
) -> str:
    path = directory / ".mcp.json"
    env = {"SWITCHBOARD_URL": url, "SWITCHBOARD_WORKSPACE": workspace}
    if url == MANAGED_HUB_URL:
        # Only ever the published constant, and only for the hub it belongs to.
        # This file is committed, so a secret token written here would be a
        # secret published — the reason this is a hardcoded comparison rather
        # than "whatever token we happen to hold".
        env["SWITCHBOARD_TOKEN"] = MANAGED_HUB_TOKEN
    entry: dict[str, Any] = {
        "command": "switchboard-mcp",
        "env": env,
    }
    if bootstrap:
        # This file is committed, so it runs on machines that never ran
        # `init` — a teammate's clone, a cloud session, a CI job. Assuming the
        # binary is there is what makes those need a setup step first.
        entry["command"] = "sh"
        entry["args"] = ["-c", _bootstrap_exec("switchboard-mcp")]
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            return f"left .mcp.json alone: existing file is not valid JSON ({exc})"
        servers = data.setdefault("mcpServers", {})
        if "switchboard" in servers and not overwrite:
            # Leaving the entry alone must not mean leaving it *broken*. A repo
            # set up before the hub required a token has none, and the hub now
            # refuses it — so the published constant is added in place while
            # everything the user may have customised (command, args, url,
            # workspace) is left exactly as it was.
            existing = servers["switchboard"].setdefault("env", {})
            if url == MANAGED_HUB_URL and "SWITCHBOARD_TOKEN" not in existing:
                existing["SWITCHBOARD_TOKEN"] = MANAGED_HUB_TOKEN
                path.write_text(json.dumps(data, indent=2) + "\n")
                return ("added the hub's public token to .mcp.json; left the rest "
                        "alone, a \"switchboard\" server was already registered")
            return 'left .mcp.json alone: a "switchboard" server is already registered'
        repointed = "switchboard" in servers
        servers["switchboard"] = entry
        if repointed:
            path.write_text(json.dumps(data, indent=2) + "\n")
            return f"repointed .mcp.json at workspace {workspace}"
    else:
        data = {"mcpServers": {"switchboard": entry}}
    path.write_text(json.dumps(data, indent=2) + "\n")
    return "wrote .mcp.json"


def _sync_hook(
    data: dict[str, Any],
    event: str,
    current_cmd: str,
    history: Sequence[Callable[[str, str], str]],
    url: str,
    workspace: str,
    force: bool,
    confirm: Callable[[str], bool] | None = None,
) -> str:
    """Add, update, or leave alone the switchboard command for one hook
    event. An existing command gets replaced only if it's untouched output
    from a past `init` run (matches `current_cmd` or a rendering of a past
    revision from `history`), `--force` was passed, or `confirm` says the
    operator approved it — anything else is presumed hand-edited and left
    alone."""
    entries = data.setdefault("hooks", {}).setdefault(event, [])
    for group in entries:
        for h in group.get("hooks", []):
            cmd = h.get("command", "")
            if "switchboard" not in cmd:
                continue
            if cmd == current_cmd:
                return f"left the {event} hook alone: already up to date"
            historical = {template(url, workspace) for template in history}
            if force or cmd in historical:
                h["command"] = current_cmd
                return f"updated the {event} hook to the latest revision"
            if confirm and confirm(f"the {event} hook"):
                h["command"] = current_cmd
                return f"overwrote your edited {event} hook, as you confirmed"
            return (
                f"left the {event} hook alone: it doesn't match a known "
                "switchboard revision (looks hand-edited) — pass --force to overwrite anyway"
            )
    entries.append({"hooks": [{"type": "command", "command": current_cmd}]})
    return f"added the {event} hook"


#: Past revisions of each hook script, for the same reason the command
#: histories exist. Empty today — the scripts were only just introduced.
_HOOK_SCRIPT_HISTORY: dict[str, list[Callable[[str, str], str]]] = {
    "session-start": [
        lambda url, ws: _hook_script_v1(_SESSION_START_BODY_V1, url, ws),
    ],
    "stop": [
        lambda url, ws: _hook_script_v1(_STOP_BODY_V1, url, ws),
    ],
}


def _hook_script_v1(body: str, url: str, workspace: str) -> str:
    """A script as `init` wrote it before the bootstrap prefix existed."""
    return (
        "#!/bin/sh\n"
        "# Generated by `switchboard init`. Safe to delete; it will come back\n"
        "# on the next run. If you edit it, `init` will leave it alone from\n"
        "# then on and tell you so.\n"
        f"{_hook_env_prefix(url, workspace).rstrip()}\n"
        f"{body}\n"
    )


def _hooks_are_gitignored(directory: Path) -> bool:
    """Whether git would refuse to commit the hook scripts.

    Splitting the bodies out of the agent's config bought unambiguous
    ownership at the cost of a second file that now *has* to travel with the
    repo: the registered shim is committed, so a clone without the scripts
    gets hooks pointing at nothing. That failure is quiet — the shim ends in
    `|| true`, so a missing script prints one line to stderr and the session
    continues as if coordination were working.

    This is `_ensure_gitignored` inverted. That one exists because writing a
    secret into a committed file is bad; this one because *not* committing
    these is. Both are cheap to check and invisible when wrong, so both get
    checked on every run rather than only at creation. `git check-ignore`
    rather than reading `.gitignore` so a global exclude — the kind Claude
    Code writes — counts too.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "check-ignore", "-q", f"{_HOOKS_DIR}/session-start.sh"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # 0 = ignored, 1 = not ignored, 128 = not a git repo (nothing to warn about)
    return result.returncode == 0


def _init_hook_scripts(
    directory: Path, url: str, workspace: str, *, force: bool = False,
    confirm: Callable[[str], bool] | None = None, bootstrap: bool = True,
) -> list[str]:
    """Write the hook bodies switchboard owns.

    Unlike the agent config these files have exactly one author, so the only
    question is whether *this user* edited them — no guessing about which of
    several tools wrote a shared file.
    """
    steps: list[str] = []
    # Keyed by the path each one lands at, because not everything switchboard
    # owns here is a hook. The listener is invoked by the agent rather than by
    # the runner's lifecycle, so it sits beside `hooks/` instead of inside it —
    # same authorship, same upgrade rules, different caller.
    scripts = {
        f"{_HOOKS_DIR}/session-start.sh": _session_start_script(url, workspace, bootstrap),
        f"{_HOOKS_DIR}/stop.sh": _stop_script(url, workspace, bootstrap),
        _WAKE_REL: _wake_script(url, workspace, bootstrap),
    }
    for label, current in scripts.items():
        name = label.rsplit("/", 1)[-1].removesuffix(".sh")
        path = directory / label
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(current)
            # It has a shebang, so make it mean something: a runner that
            # invokes the path directly rather than via `sh` still works.
            path.chmod(0o755)
            steps.append(f"wrote {label}")
            continue
        history = [t(url, workspace) for t in _HOOK_SCRIPT_HISTORY.get(name, [])]
        status = _revision_status(path.read_text(), current, history)
        if status == "current":
            steps.append(f"left {label} alone: already up to date")
        elif status == "stale" or force:
            path.write_text(current)
            steps.append(f"updated {label} to the latest revision")
        elif confirm and confirm(f"your edited {label}"):
            path.write_text(current)
            steps.append(f"overwrote {label}, as you confirmed")
        else:
            steps.append(
                f"left {label} alone: it doesn't match a known switchboard "
                "revision (looks hand-edited) — pass --force to overwrite anyway"
            )
    if _hooks_are_gitignored(directory):
        steps.append(
            f"note: {_HOOKS_DIR}/ is gitignored, but the hooks that call it are "
            "committed. A clone would get hooks pointing at scripts that are not "
            "there, and the failure is quiet — nothing here is secret, so let "
            "these be committed"
        )
    return steps


def _init_claude_settings(
    directory: Path, url: str, workspace: str, *, force: bool = False,
    confirm: Callable[[str], bool] | None = None,
) -> list[str]:
    path = directory / ".claude" / "settings.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            return [f"left .claude/settings.json alone: existing file is not valid JSON ({exc})"]
    else:
        data = {}
    steps = [
        _sync_hook(
            data, "SessionStart", _session_start_cmd(url, workspace),
            _SESSION_START_CMD_HISTORY, url, workspace, force, confirm,
        ),
        _sync_hook(
            data, "Stop", _stop_cmd(url, workspace),
            _STOP_CMD_HISTORY, url, workspace, force, confirm,
        ),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return steps


#: Where `init` puts the workspace key. Deliberately not `.mcp.json`: that
#: file has to be committed for a cloud session or CI runner to find the MCP
#: server at all, and a key committed to git is not a key. Claude Code applies
#: this file's `env` block to the session *and* to the subprocesses it spawns,
#: which is what gets `SWITCHBOARD_KEY` to `switchboard-mcp` without anyone
#: having to `export` it by hand. `.env` would stay out of git just as well
#: but nothing loads it into that subprocess — it works for the local dev
#: token only because `docker compose` reads `.env` itself.
#: Named in config.py, where `from_repo` reads it too.
_LOCAL_SETTINGS_REL = LOCAL_SETTINGS_FILE
_GITIGNORE_PATTERN = "**/.claude/settings.local.json"


def _ensure_gitignored(directory: Path) -> str | None:
    """Make sure the local settings file is ignored, returning a step to
    report if we had to change something.

    Claude Code adds this pattern to your *global* git excludes, but only for
    a file it wrote itself. `init` writes this one by hand, so that safety net
    does not apply and we have to place the entry ourselves. Getting this
    wrong means writing a secret into a file the user then commits, so it is
    checked on every run rather than only at creation.
    """
    path = directory / ".gitignore"
    existing = path.read_text() if path.exists() else ""
    lines = {line.strip() for line in existing.splitlines()}
    if lines & {_GITIGNORE_PATTERN, ".claude/settings.local.json", ".claude/"}:
        return None
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with path.open("a") as f:
        f.write(f"{prefix}{_GITIGNORE_PATTERN}\n")
    return f"added {_GITIGNORE_PATTERN} to .gitignore"


def _hub_reaches_workspace(url: str, token: str, workspace: str) -> bool | None:
    """Whether `token` may act in `workspace`. None when the hub is unreachable.

    Any authenticated read answers this; presence is the cheapest and creates
    nothing.
    """
    import httpx

    try:
        response = httpx.get(
            f"{url}/agents",
            params={"workspace": workspace},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
    except httpx.HTTPError:
        return None
    return response.status_code < 400


def _init_hub_token(
    directory: Path, url: str, workspace: str, token: str | None, *, force: bool
) -> tuple[list[str], str | None]:
    """Check that this workspace is actually reachable, and say so if not.

    `init` used to be able to *fix* an unreachable workspace by registering a
    token for it. Nothing registers anything now — a room identifier is
    derived from its token rather than claimed — so an unreachable workspace
    means the hub wants a credential this environment does not have, and only
    whoever runs it can supply one.

    Network failures are reported, never fatal: `init` is a wiring tool and the
    files it writes are correct with or without a reachable hub.
    """
    if not token:
        return [], None
    reachable = _hub_reaches_workspace(url, token, workspace)
    if reachable is not False:
        return [], token
    return [
        f"note: this token cannot reach workspace {workspace!r} on {url}. Ask "
        "whoever runs that hub for one that can, or point at a hub you run "
        "yourself with --local"
    ], token


#: What each secret `init` may write is called when explaining itself, and why
#: replacing an existing one without --force is refused.
_LOCAL_SECRETS = {
    "SWITCHBOARD_KEY": (
        "key",
        "Replacing it silently would cut this agent off from everyone still "
        "using the old one",
    ),
    "SWITCHBOARD_TOKEN": (
        "hub token",
        "Replacing it silently would drop this agent's access to whatever the "
        "old one reached",
    ),
}


def _init_local_setting(
    directory: Path, name: str, value: str, *, force: bool
) -> tuple[list[str], bool]:
    """Record a secret in the gitignored local settings file.

    Refuses to replace a *different* existing value without --force. Silently
    swapping one is the worst available failure for either secret: nothing
    errors, and the agent quietly stops reaching whoever it used to.
    """
    label, why = _LOCAL_SECRETS[name]
    steps: list[str] = []
    ignored = _ensure_gitignored(directory)
    if ignored:
        steps.append(ignored)

    path = directory / _LOCAL_SETTINGS_REL
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            steps.append(f"left {_LOCAL_SETTINGS_REL} alone: not valid JSON ({exc})")
            return steps, False
    else:
        data = {}
    env = data.setdefault("env", {})
    existing = env.get(name)
    if existing == value:
        steps.append(f"left {_LOCAL_SETTINGS_REL} alone: this {label} is already set")
        return steps, True
    if existing and not force:
        steps.append(
            f"left {_LOCAL_SETTINGS_REL} alone: a different {name} is already "
            f"set. {why} — pass --force if that is what you want"
        )
        return steps, False
    env[name] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    steps.append(
        f"{'replaced the ' + label + ' in' if existing else 'wrote the ' + label + ' to'} "
        f"{_LOCAL_SETTINGS_REL}"
    )
    return steps, True


def _init_key(directory: Path, key: str, *, force: bool) -> tuple[list[str], bool]:
    return _init_local_setting(directory, "SWITCHBOARD_KEY", key, force=force)


_SKILL_NAME = SKILL_NAME

# The skill's equivalent of the two hook-command history lists above. Both
# accessors live in `guidance.py` because `help` and the MCP bridge serve the
# same text and must not import the CLI to read a file; these names stay as
# the spelling the init code and its tests already use.
_skill_history = skill_history
_skill_source = skill_text


def _init_skill(
    directory: Path, *, force: bool = False, confirm: Callable[[str], bool] | None = None
) -> str:
    path = directory / ".claude" / "skills" / _SKILL_NAME / "SKILL.md"
    current = _skill_source()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(current)
        return f"installed the {_SKILL_NAME} skill"
    label = path.relative_to(directory)
    status = _revision_status(path.read_text(), current, _skill_history())
    if status == "current":
        return f"left {label} alone: already up to date"
    if status == "stale" or force:
        path.write_text(current)
        return f"updated the {_SKILL_NAME} skill to the latest revision"
    if confirm and confirm(f"your edited {label}"):
        path.write_text(current)
        return f"overwrote {label}, as you confirmed"
    return (
        f"left {label} alone: it doesn't match a known switchboard revision "
        "(looks hand-edited) — pass --force to overwrite anyway"
    )


def _init_claude_md(
    directory: Path, *, force: bool = False, confirm: Callable[[str], bool] | None = None
) -> str:
    path = directory / "CLAUDE.md"
    if not path.exists():
        path.write_text("# CLAUDE.md\n\n" + _CLAUDE_MD_SECTION)
        return "wrote CLAUDE.md"
    text = path.read_text()
    marker_at = text.find(_CLAUDE_MD_MARKER)
    if marker_at == -1:
        sep = "\n" if text.endswith("\n\n") else ("\n\n" if text.endswith("\n") else "\n\n\n")
        path.write_text(text + sep + _CLAUDE_MD_SECTION)
        return "appended a coordination section to CLAUDE.md"
    existing = text[marker_at:]
    status = _revision_status(existing, _CLAUDE_MD_SECTION, _CLAUDE_MD_SECTION_HISTORY)
    if status == "current":
        return "left CLAUDE.md alone: coordination section is already up to date"
    if status == "stale" or force:
        path.write_text(text[:marker_at] + _CLAUDE_MD_SECTION)
        return "updated CLAUDE.md's coordination section to the latest revision"
    if confirm and confirm("your edited coordination section in CLAUDE.md"):
        path.write_text(text[:marker_at] + _CLAUDE_MD_SECTION)
        return "overwrote CLAUDE.md's coordination section, as you confirmed"
    return (
        "left CLAUDE.md alone: its coordination section doesn't match a known "
        "switchboard revision (looks hand-edited) — pass --force to overwrite anyway"
    )


def cmd_init(args: argparse.Namespace) -> int:
    """Wire up a repo end to end: .mcp.json, lifecycle hooks, CLAUDE.md, the
    coordination skill, a dev token."""
    arg_url = getattr(args, "init_url", None) or args.url
    quiet = getattr(args, "init_quiet", False) or args.quiet
    as_json = getattr(args, "init_json", False) or args.json
    arg_token = getattr(args, "init_token", None) or args.token
    if args.local and arg_url:
        print("error: --local and --url are mutually exclusive — pick one.", file=sys.stderr)
        return EXIT_ERROR

    given = getattr(args, "init_key", None) or args.key
    if args.new_key and given:
        print(
            "error: --new-key and --key are mutually exclusive — mint a key or adopt "
            "one, not both.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if args.no_key and (given or args.new_key):
        print(
            "error: --no-key cannot be combined with --key or --new-key.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    directory = Path(args.dir or ".").resolve()
    arg_workspace = getattr(args, "init_workspace", None) or args.workspace
    explicit_workspace = bool(arg_workspace or os.environ.get("SWITCHBOARD_WORKSPACE"))
    workspace = arg_workspace or os.environ.get("SWITCHBOARD_WORKSPACE") or _default_workspace(
        directory
    )

    # Encryption is the default posture, so a repo that has no key gets one.
    # The alternative — plaintext unless someone knew to ask — meant the
    # privacy of a workspace depended on having read the right doc first.
    #
    # Every already-known key wins over minting, including the one this repo
    # may already hold on disk: minting a second key for a repo that has one
    # is the divergence `_init_key` exists to refuse, and doing it by default
    # would be doing it constantly.
    key = given or os.environ.get("SWITCHBOARD_KEY") or _saved_key(directory)
    minted: str | None = None
    if args.no_key:
        key = None
    elif args.new_key or not key:
        minted = key = generate_key()
        # `--new-key` also swaps in an opaque workspace name, for the reason
        # `keygen` explains: the workspace is the one thing the hub always
        # sees in the clear. Minting by default does *not* — a derived
        # `org/repo` is what makes a laptop, a clone and CI agree for free,
        # and silently replacing it with an opaque string would trade that
        # away for a privacy win nobody asked for at that moment.
        if args.new_key and not explicit_workspace:
            workspace = "w_" + generate_key()[:16]
    if args.local:
        url = "http://127.0.0.1:8787"
    else:
        url = (arg_url or os.environ.get("SWITCHBOARD_URL") or MANAGED_HUB_URL).rstrip("/")
    local_hub = url in _LOCAL_HUB_URLS
    managed_hub = url == MANAGED_HUB_URL and not local_hub

    interactive = _can_prompt(
        no_input=getattr(args, "no_input", False), quiet=quiet, as_json=as_json
    )

    steps: list[str] = []
    # A key seals messages; the workspace decides who you are sealed *with*.
    # When .mcp.json already routes somewhere else, the two disagree and the
    # failure is silent — everything encrypts fine and the agents never meet.
    # The file is what actually routes, so it wins unless someone says
    # otherwise.
    repoint_mcp = False
    # Read .mcp.json even under --skip-mcp. That flag says do not *write* the
    # file; it is still what routes this repo, and pairing a key against a
    # workspace the file contradicts is precisely the silent failure this
    # block exists to prevent. What --skip-mcp does remove is the option to
    # repoint — we cannot change where the repo routes without writing it —
    # so there the file simply wins.
    registered = _mcp_workspace(directory)
    if key and registered and registered != workspace:
        if args.skip_mcp:
            workspace = registered
            steps.append(
                f"paired the key with workspace {registered!r} from .mcp.json — "
                "--skip-mcp means the file was read but not changed"
            )
        elif interactive:
            choice = _ask_choice(
                f"\n.mcp.json already routes this repo to workspace {registered!r}, "
                f"but the key is being paired with {workspace!r}. They have to match.",
                [
                    (f"keep {registered}", "leave .mcp.json alone, pair the key with it"),
                    (f"switch to {workspace}", "repoint .mcp.json at the new workspace"),
                ],
                default=0,
            )
            if choice == 0:
                workspace = registered
            else:
                repoint_mcp = True
        elif args.force:
            repoint_mcp = True
        elif explicit_workspace:
            # They named a workspace and the file names another. Guessing
            # either way silently is how you get two half-configured repos.
            steps.append(
                f"note: you asked for workspace {workspace!r} but .mcp.json routes to "
                f"{registered!r}, so this repo's agents will use {registered!r} and the "
                "key will not reach the room you meant. Re-run with --force to repoint "
                "it, or drop -w to adopt the registered one"
            )
            workspace = registered
        else:
            # Nothing to honour but the file itself — adopt it, and say so,
            # because it overrides the opaque name --new-key just minted.
            workspace = registered
            steps.append(
                f"paired the key with workspace {registered!r}, already registered in "
                ".mcp.json (pass --force to repoint it at a fresh opaque name instead)"
            )

    def confirm_edit(what: str) -> bool:
        # Default no: the safe answer for someone's own edit is to keep it.
        return _ask(
            f"\n{what} doesn't match a known switchboard revision — it looks "
            "hand-edited.\noverwrite it with the current version?",
            default=False,
        )

    confirm = confirm_edit if interactive else None

    token: str | None = None
    if local_hub:
        token, msg = _init_token(directory, arg_token)
        steps.append(msg)
    if not args.skip_mcp:
        steps.append(_init_mcp_json(
            directory, url, workspace, overwrite=repoint_mcp,
            bootstrap=not args.no_bootstrap,
        ))
    if not args.skip_hooks:
        # Bodies first, then the registration that points at them — so a
        # half-finished run never leaves a hook calling a script that is not
        # there yet.
        steps.extend(
            _init_hook_scripts(
                directory, url, workspace, force=args.force, confirm=confirm,
                bootstrap=not args.no_bootstrap,
            )
        )
        steps.extend(
            _init_claude_settings(
                directory, url, workspace, force=args.force, confirm=confirm
            )
        )
    if not args.skip_claude_md:
        steps.append(_init_claude_md(directory, force=args.force, confirm=confirm))
    if not args.skip_skill:
        steps.append(_init_skill(directory, force=args.force, confirm=confirm))
    key_ok = True
    if key:
        key_steps, key_ok = _init_key(directory, key, force=args.force)
        steps.extend(key_steps)

    # Last, and only against a remote hub: a token that cannot act in this
    # workspace makes everything above correct and useless. `--local` has its
    # own token flow and a resolver with no concept of workspace scoping.
    token_on_disk = False
    if not local_hub and not args.skip_token:
        before = token or arg_token or os.environ.get("SWITCHBOARD_TOKEN")
        token_steps, token = _init_hub_token(
            directory, url, workspace, before, force=args.force
        )
        steps.extend(token_steps)
        token_on_disk = bool(token) and token != before

    local_hub_note = (
        "this hub is only reachable from this machine — a cloud session or CI runner "
        "pointed at it would start its own separate, empty hub and never see agents "
        "here. To coordinate across machines, deploy one shared hub (see "
        "docs/deployment.md) and re-run `switchboard init --url https://your-hub` so "
        "that URL is what gets committed, or self-host locally with `--local`."
    )
    # Only reachable with --no-key now. It used to say the managed hub is "a
    # shared public hub with one token everyone uses" where "every other user
    # can read and post to your workspace" — true of a shared-token
    # deployment, and false of this one: it scopes each token to a single
    # workspace, so a stranger's token gets a 401 rather than your messages.
    # Overstating who can read you is not a safe kind of wrong; it teaches
    # people to distrust the wrong thing.
    managed_hub_note = (
        f"this workspace is not encrypted. On {MANAGED_HUB_URL} a token is bound "
        "to one workspace, so other users cannot read yours — but the operator "
        "runs the hub and everything reaching it is in the clear to them: message "
        "bodies, board values, lease notes, branch names.\n"
        "  `switchboard init` seals all of that by default; you passed --no-key. "
        "Re-run it without that flag to mint a key, or `--new-key` to also replace "
        "the workspace name with an opaque one so the hub cannot tell what the "
        "workspace is about either.\n"
        "  What a key never hides is metadata — the hub still sees message timing, "
        "volume, and how many agents you run. If that matters, self-host: "
        "`switchboard init --local` (or `--url` to point at a hub you already "
        "deployed)."
    )
    # Just the privacy claim. This used to carry setup instructions too, which
    # the explainer at the end now gives — and gave differently, telling you to
    # pass -w and to set SWITCHBOARD_WORKSPACE in a cloud environment, both of
    # which pin a machine to a workspace it should be reading from the repo.
    sealed_note = (
        f"this workspace is sealed with a key held only on this machine, so "
        f"neither other users of {MANAGED_HUB_URL} nor whoever runs it can read "
        "it. The hub still sees message timing, volume, and how many agents you "
        "run — a key hides content, not metadata. If that matters, self-host: "
        "`switchboard init --local` (or `--url` to point at a hub you already "
        "deployed)."
    )
    if key and key_ok and managed_hub:
        managed_hub_note = sealed_note
    # A key without a matching workspace name is the quiet failure: sealing
    # works, routing does not, and the two agents just never meet.
    #
    # But adopting a key without -w is two different acts, and only one of
    # them is a mistake. Joining a teammate means you must also match the
    # workspace they use. Adding another of your own repos under one key —
    # the only shape that works when a cloud environment holds a single
    # SWITCHBOARD_KEY, see docs/environments.md — means a *different*
    # workspace per repo is the whole point. `init` cannot read intent, but
    # it can tell whether the name it derived is one other clones will derive
    # too, which is what separates "this is fine" from "nobody else will ever
    # land here".
    if key and not minted and not explicit_workspace and not registered:
        if _git_remote_workspace(directory):
            steps.append(
                f"note: this repo's workspace is {workspace!r}, from its git remote, and "
                "the key is now paired with it. That is what you want when you are adding "
                "another of your own repos under one key. If the key came from someone "
                "else, you need their workspace name too — pass -w"
            )
        else:
            steps.append(
                f"note: no git remote here, so the workspace defaulted to {workspace!r} — "
                "a name derived from this machine that no other agent will arrive at on "
                "its own. Anything that should see this repo's agents needs -w with that "
                "exact name"
            )

    if as_json:
        payload: dict[str, Any] = {"workspace": workspace, "url": url, "steps": steps}
        if local_hub:
            payload["note"] = local_hub_note
        elif managed_hub:
            payload["note"] = managed_hub_note
        if minted and key_ok:
            payload["key"] = minted
        _print_json(payload)
        return EXIT_OK if key_ok else EXIT_ERROR

    fmt = Fmt(_use_color(sys.stdout))
    if not quiet:
        print(fmt.bold(f"switchboard init — workspace {fmt.cyan(workspace)}, hub {url}"))
        for step in steps:
            if step.startswith("left "):
                marker = fmt.dim("·")
            elif step.startswith("note:"):
                marker = fmt.yellow("!")
            else:
                marker = fmt.green("+")
            print(f"  {marker} {step}")
        if local_hub:
            print()
            print(fmt.yellow("Note: ") + local_hub_note)
        elif managed_hub:
            print()
            print(fmt.yellow("Note: ") + managed_hub_note)
        print()
        print(fmt.bold("Next"))
        n = 1
        if local_hub:
            print(f"  {n}. start the hub — either:")
            print("       docker compose up -d          # reads the token from .env")
            print("     or:")
            print(f"       export SWITCHBOARD_TOKEN={token}")
            print("       switchboard serve")
            n += 1
        if local_hub:
            print(f"  {n}. export SWITCHBOARD_TOKEN={token} in every agent's shell")
            n += 1
        elif not _mcp_env(directory, "SWITCHBOARD_TOKEN"):
            # Registration is gone, so nothing here can obtain a token. Only
            # whoever runs the hub can, and saying otherwise sends people to
            # run a command that will never produce one.
            print(f"  {n}. this hub needs a token and this repo has none: ask "
                  "whoever runs it, then set SWITCHBOARD_TOKEN in your "
                  "environment")
            n += 1
        # When the token *is* on disk there is nothing to do: the MCP
        # subprocess reads it from there, and the explainer below covers
        # handing it to another environment.
        print(f"  {n}. restart Claude Code and check `/mcp` — switchboard should show connected")
        if minted and key_ok:
            # Printed once, and never again: it is on disk but `init` will not
            # read it back out to show you, and the hub cannot recover it.
            print()
            # Deliberately not "copy it now, it is not shown again": the key is
            # sitting in _LOCAL_SETTINGS_REL in the clear, so that would be
            # false, and the way it goes wrong is expensive. Someone who
            # scrolls past believes the key is gone, re-runs --new-key, and
            # silently cuts themselves off from every teammate still holding
            # the old one — the exact divergence `_init_key` refuses to cause
            # on its own. Say where it lives instead. The real risk is losing
            # the *file*, which is a back-it-up problem, not a memorize-it one.
            print(fmt.bold("Your new key:"))
            print(f"  {minted}")
            print(
                f"  Give teammates: switchboard init --key {minted} -w {workspace}"
            )
            # Where it is saved is the explainer's line now. What survives here
            # is the part nothing else says: the hub cannot get it back for you.
            print(
                "  Saved on disk, and `switchboard whoami --show-key` prints it "
                "again — but the hub never receives it, so nobody there can read "
                "this workspace or recover the key if you lose the file."
            )
        if not local_hub:
            _print_scope_explainer(fmt, minted if key_ok else None, token_on_disk)
    return EXIT_OK if key_ok else EXIT_ERROR


def _print_scope_explainer(fmt: Fmt, minted: str | None, token_on_disk: bool) -> None:
    """Say which part of this setup a clone brings and which it does not.

    The split is committed versus secret, not repo versus machine. Both halves
    sit in the repo — the secrets in a gitignored file — which is why a second
    repo has to be given the key even on the same machine, and why calling
    that half "the machine" was wrong: it reads as though the machine already
    has it.

    Kept to a handful of lines on purpose. An earlier version explained the
    whole model and was long enough that skimming past it was the likely
    outcome, which is the same as not printing it.
    """
    secrets = "key + token" if token_on_disk else "key"
    print()
    print(fmt.bold("Two halves"))
    print(f"  {fmt.cyan('committed')}  .mcp.json, .switchboard/hooks — hub URL and "
          "workspace; every clone gets these")
    print(f"  {fmt.cyan('secret')}     {secrets} in {_LOCAL_SETTINGS_REL} — gitignored, "
          "and written per repo")
    print()
    # Per repo, so a second one needs the key handed to it even here. Exporting
    # it once is the alternative, and worth naming: `init` reads the
    # environment, so people who set it up that way never touch --key again.
    print("  another repo      switchboard init --key <key>   (or export "
          "SWITCHBOARD_KEY first)")
    # The package first: without it `switchboard-mcp` is not on PATH and the
    # MCP server never starts, so the secrets have nothing to reach the hub
    # with. Two steps because it is genuinely two, and leaving one out is how
    # a cloud session ends up with no switchboard tools and no clue why.
    # [crypto], not the bare package: `cryptography` is an optional extra, and
    # a workspace with a key — which `--new-key` always produces — makes the
    # MCP server raise CryptoError at startup without it. Recommending the
    # bare install alongside a flow that mints a key was a broken instruction.
    extra = "'agent-switchboard[crypto]'" if minted or token_on_disk else "agent-switchboard"
    print(f"  another machine   1. pip install {extra}   "
          "2. paste `switchboard whoami --env`")
    # A cloud environment is the one `init` cannot do anything about: its
    # configuration lives outside every repo, so nothing written into a
    # checkout reaches it. Saying so is the whole value here — the alternative
    # is a cloud session with no package, no tools, and nothing that looks
    # like a failure.
    #
    # Split into two lines because they are two fields in the environment's
    # settings, filled separately: a setup script that runs before the agent
    # starts, and a list of variables. Printing them as one block of shell —
    # `pip install` next to a couple of `export`s — is a block that cannot be
    # pasted anywhere as-is, and the reader has to take it apart first.
    # `--ignore-installed cryptography`, and only on this line. A container
    # image is where a *system* cryptography lives — Debian ships one — and
    # pip refuses to replace a package it did not install ("RECORD file not
    # found"), so a plain install exits 1, the setup script fails, and the
    # environment comes up with no switchboard and nothing that explains why.
    # Scoped to the one package that collides: pip stops trying to uninstall
    # the system copy and puts its own in /usr/local, which wins on sys.path.
    # The bare package, not an extra: it already carries cryptography and
    # `switchboard-mcp`, while `[all]` would drag in FastAPI and uvloop for an
    # agent that will never run a hub — more to download and more to collide.
    print("  a cloud env       two boxes, neither of them here:\n"
          "                      setup script  pip install --ignore-installed "
          "cryptography agent-switchboard\n"
          "                      env vars      SWITCHBOARD_KEY, SWITCHBOARD_TOKEN "
          "(from `whoami --env`)")


# --- parser -----------------------------------------------------------------


#: Global options that also work *after* the subcommand. `switchboard say
#: general hi --json` is what everyone types first, and argparse's answer was
#: "unrecognized arguments" — including for `-q` and `--url`, and including in
#: places switchboard's own output tells you to type them. It cost a real
#: agent its roster presence mid-run: `announce --json` failed, so it never
#: registered, and nothing about the error said the flag was merely misplaced.
_GLOBAL_FLAGS: tuple[tuple[tuple[str, ...], dict[str, Any]], ...] = (
    (("--url",), {}),
    (("--token",), {}),
    (("-w", "--workspace"), {}),
    (("--agent-id",), {}),
    (("--key",), {}),
    (("--invite",), {}),
    (("--json",), {"action": "store_true"}),
    (("-q", "--quiet"), {"action": "store_true"}),
)


def _accept_global_flags_after_subcommand(parser: argparse.ArgumentParser) -> None:
    """Let every subcommand take the global flags too, in either position.

    The trick is `default=SUPPRESS`: a subparser option sharing a dest with
    its parent normally *overwrites* the parent's value with its own default
    whenever the flag is not repeated after the subcommand — the exact trap
    `init` sidestepped with separate dests and a note. Suppressed defaults
    leave the attribute unset instead, so the parent's value survives and one
    dest serves both positions, with no command needing to read two.

    `init` keeps its bespoke handling: its options are already declared with
    their own dests, and re-declaring the same strings here would be an
    argparse conflict. Nested subcommands (`board set`) are walked too, since
    that is where the flag lands when a command has its own subcommands.
    """
    seen: set[int] = set()

    def walk(p: argparse.ArgumentParser, name: str | None) -> None:
        if id(p) in seen:  # aliases (announce/register) share one parser
            return
        seen.add(id(p))
        if name is not None and name != "init":
            for flags, options in _GLOBAL_FLAGS:
                p.add_argument(
                    *flags, default=argparse.SUPPRESS, help=argparse.SUPPRESS, **options
                )
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child_name, child in action.choices.items():
                    walk(child, child_name)

    walk(parser, None)


#: Verbs an agent reaches for that this CLI does not have, and what it wanted.
#:
#: Evidence-driven rather than imagined: `send` is here because two different
#: agents, in two different sessions, typed `switchboard send` — it is the
#: natural word for "put this message somewhere" when the verbs on offer are
#: `say`, `dm` and `whisper`. Nothing in the docs ever suggested it. The rest
#: are the same shape, including the MCP tool names, because an agent that
#: read the skill knows `roster` and `board_set` and then finds itself at a
#: CLI that spells them differently.
#:
#: The point is not to accept these — a guessed verb should not silently post
#: to a channel named after an agent — but to answer the question the typo
#: asked, in the sentence where the mistake is discovered.
_MISTAKEN_VERBS = {
    "send": "say <channel> to post where the room can read it, "
            "or dm <agent> for one peer",
    "post": "say <channel>",
    "publish": "say <channel>",
    "broadcast": "say <channel>",
    "message": "dm <agent>, or say <channel>",
    "msg": "dm <agent>, or say <channel>",
    "tell": "dm <agent>",
    "notify": "dm <agent>",
    "read": "inbox",
    "roster": "agents",
    "who": "agents",
    "board_set": "board set",
    "board_get": "board get",
    "join_room": "join",
}


class _Parser(argparse.ArgumentParser):
    """An argument parser that answers "then what should I have typed?".

    argparse's own reply to an unknown subcommand is `invalid choice: 'send'`
    followed by every command this CLI has, which is a list to search rather
    than an answer. For an agent that is a wasted round trip, and it is a
    round trip taken while it is trying to tell somebody something.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        match = re.search(r"invalid choice: '([^']+)'", message)
        if match:
            typed = match.group(1)
            hint = _MISTAKEN_VERBS.get(typed)
            if hint is None:
                choices: list[str] = []
                for action in self._subparsers._group_actions if self._subparsers else []:
                    choices.extend(getattr(action, "choices", None) or [])
                near = difflib.get_close_matches(typed, choices, n=2, cutoff=0.7)
                hint = " or ".join(near) if near else None
            if hint:
                print(
                    f"switchboard: there is no `{typed}` command. Did you mean "
                    f"{hint}?",
                    file=sys.stderr,
                )
                # 2, which is what argparse exits with for every other usage
                # error. It collides numerically with EXIT_CONFLICT, and that
                # is argparse's convention rather than ours to change here.
                self.exit(2)
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="switchboard",
        description="Ephemeral orchestration hub for AI coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"switchboard {__version__}")
    parser.add_argument("--url", help="hub base URL (env: SWITCHBOARD_URL)")
    parser.add_argument("--token", help="bearer token (env: SWITCHBOARD_TOKEN)")
    parser.add_argument("-w", "--workspace", help="workspace (env: SWITCHBOARD_WORKSPACE)")
    parser.add_argument("--agent-id", help="override this agent's id")
    parser.add_argument(
        "--key",
        help="key for end-to-end encryption (env: SWITCHBOARD_KEY). "
             "Never sent to the hub; generate one with `switchboard keygen`.",
    )
    parser.add_argument(
        "--lobby", action="store_true",
        help="run this command in the room every holder of this key shares. "
             "Derived from the key rather than agreed by name, so two agents "
             "in unrelated checkouts meet without either being told a "
             "workspace — and nobody without the key can find it.",
    )
    parser.add_argument(
        "--invite",
        help="run this one command in the room an invite describes (from "
             "`switchboard invite`). Hub, workspace, token and key come from the "
             "string and outrank the environment; nothing on disk is changed.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress success chatter")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run a hub")
    p.add_argument("--cors-origin", action="append", default=[], metavar="ORIGIN",
                   help="allow a browser page on ORIGIN to read this hub; repeatable "
                        "(env: SWITCHBOARD_CORS_ORIGINS). Off by default — a hub with "
                        "no browser client should not carry the attack surface of one. "
                        "`*` is allowed and means it: this API is authenticated by a "
                        "header, not a cookie, so a page that cannot present a token "
                        "gains nothing")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--db", help="SQLite path (env: SWITCHBOARD_DB)")
    p.add_argument("--log-level", default="info")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser(
        "init",
        help="wire up this repo: .mcp.json, lifecycle hooks, CLAUDE.md, "
             "the coordination skill, a dev token",
    )
    p.add_argument("--dir", help="target repo directory (default: current directory)")
    p.add_argument(
        "--local",
        action="store_true",
        help="self-host instead of using the managed hub: point .mcp.json at "
             "http://127.0.0.1:8787 and generate a dev token. Shorthand for "
             "--url http://127.0.0.1:8787. Mutually exclusive with --url.",
    )
    # `init` accepts the connection options after the subcommand as well as
    # before it. They are global options, so `switchboard init -w foo` was an
    # "unrecognized arguments" error — including in the two places init's own
    # output tells you to type it. Separate dests so that the parent form
    # (`switchboard -w foo init`) keeps working: a subparser option sharing a
    # dest overwrites the parent's value with its own default whenever the
    # flag is not repeated after the subcommand. cmd_init reads both.
    p.add_argument("-w", "--workspace", dest="init_workspace", help=argparse.SUPPRESS)
    p.add_argument("--url", dest="init_url", help=argparse.SUPPRESS)
    p.add_argument("--token", dest="init_token", help=argparse.SUPPRESS)
    p.add_argument("-q", "--quiet", dest="init_quiet", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--json", dest="init_json", action="store_true", help=argparse.SUPPRESS)
    # A separate dest from the global --key so that `switchboard --key K init`
    # keeps working: a subparser option sharing a dest overwrites the parent's
    # value with its own default when the flag is not repeated after the
    # subcommand. cmd_init reads both.
    p.add_argument(
        "--key", dest="init_key",
        help="adopt an existing key (env: SWITCHBOARD_KEY) — the one a "
             "teammate gave you. Written to .claude/settings.local.json, which is "
             "kept out of git. Pair it with the same -w workspace they use, or you "
             "will be sealed correctly and routed somewhere else.",
    )
    p.add_argument(
        "--no-key", action="store_true",
        help="do not encrypt: skip minting a key. `init` mints one by "
             "default, so bodies, board values, lease notes and branch names are "
             "sealed before they leave this machine. Use this only where the hub is "
             "already trusted with plaintext.",
    )
    p.add_argument(
        "--new-key", action="store_true",
        help="mint a fresh key *and* replace the derived workspace with an opaque "
             "name, so the hub cannot read the workspace or guess what it is. `init` "
             "already mints a key when the repo has none; this additionally hides the "
             "name, and replaces a key the repo already had. Mutually exclusive "
             "with --key.",
    )
    p.add_argument(
        "--no-input", action="store_true",
        help="never stop to ask. Without it, `init` asks before overwriting a "
             "hand-edited file or repointing a repo at a different workspace — but "
             "only when there is a terminal to ask on, so agents, CI and pipelines "
             "are unaffected either way. Not the same as answering yes: the "
             "defaults are the cautious answers, and --force is what says yes.",
    )
    p.add_argument(
        "--skip-token", action="store_true",
        help="do not contact the hub to check or register a token. Without it, "
             "`init` verifies this workspace is actually reachable and, on a hub "
             "that lets clients bind their own tokens, registers one when nothing "
             "can reach it — otherwise a freshly minted workspace 403s on every "
             "call while init reports success. This is the only step that uses "
             "the network; failures are reported, never fatal.",
    )
    p.add_argument(
        "--no-bootstrap", action="store_true",
        help="write config that assumes `switchboard` is already installed. By "
             "default the committed .mcp.json and hook scripts resolve it — an "
             "installed binary first, else `uvx` from a cache — so a clone or a "
             "cloud session needs no install step. Use this where the environment "
             "pins its own version and nothing should reach the network.",
    )
    p.add_argument("--skip-mcp", action="store_true", help="do not write .mcp.json")
    p.add_argument(
        "--skip-hooks", action="store_true", help="do not write .claude/settings.json hooks"
    )
    p.add_argument("--skip-claude-md", action="store_true", help="do not touch CLAUDE.md")
    p.add_argument(
        "--skip-skill", action="store_true",
        help="do not install the switchboard-coordinate skill",
    )
    p.add_argument(
        "--force", action="store_true",
        help="overwrite CLAUDE.md's coordination section, the skill file, and the "
             "SessionStart/Stop hooks even if they don't match a known switchboard "
             "revision. Without this, anything that looks hand-edited — not just "
             "untouched output from a past `init` run — is left alone.",
    )
    p.set_defaults(func=cmd_init)

    p = sub.add_parser(
        "rooms",
        help="what this repo declares, and which of them you hold a key for",
        description="A repo declares rooms; an environment holds keys; the agent "
                    "joins the intersection. This shows both sides and, when they "
                    "do not resolve to exactly one room, says why.",
    )
    p.add_argument("--dir", help="target repo directory (default: current directory)")
    p.set_defaults(func=cmd_rooms)

    p = sub.add_parser(
        "help",
        help="print the coordination protocol — how to work alongside other agents",
        description="Print the switchboard-coordinate protocol: when to claim, how "
                    "handoffs are addressed, what a timing forecast does and does not "
                    "promise. `--help` covers the commands; this covers the convention "
                    "they are meant to implement. Reads the copy packaged with this "
                    "install and never touches the hub, so it works before setup and "
                    "when the hub is unreachable.",
    )
    p.add_argument(
        "--role",
        help="also print this repo's overlay for a role, from .switchboard/spec.json. "
             "Switchboard defines no roles of its own; an unknown one is an error "
             "rather than a silent fall back to the shared protocol.",
    )
    p.set_defaults(func=cmd_help)

    p = sub.add_parser(
        "refresh",
        help="what this repo declares about how its agents work together",
        description="The agent-invoked counterpart to `init`. `init` wires a repo up "
                    "once; this records the answers that are discovered rather than "
                    "configured — which roles exist here, what a claim is named, where "
                    "the plan lives — and tracks whether they have gone stale. "
                    "Switchboard supplies no fields of its own and never interprets "
                    "one: the repo says, this writes it down.",
    )
    rsub = p.add_subparsers(dest="refresh_action", required=True)
    r = rsub.add_parser("status", help="what is recorded, and whether it is stale")
    r.add_argument("--dir", help="repo root (default: cwd)")
    r.set_defaults(func=cmd_refresh)
    r = rsub.add_parser("set", help="record fields discovered this session")
    r.add_argument(
        "spec_json", nargs="?",
        help="JSON object of field name -> value, or `-` to read it from stdin. "
             "Pipe it: a payload passed as an argument is interpreted by your shell "
             "before Switchboard sees it.",
    )
    r.add_argument("--dir", help="repo root (default: cwd)")
    r.add_argument(
        "--provenance", choices=list(PROVENANCES), default="inferred",
        help="`inferred` (default) is something this agent worked out; `asserted` is "
             "something a human decided. An inferred value never silently replaces an "
             "asserted one — pass --force when you really mean to.",
    )
    r.add_argument(
        "--outcome", choices=list(OUTCOMES),
        help="what this refresh concluded. Defaults to `updated` when anything "
             "changed and `unchanged` otherwise. Say `failed` or `partial` explicitly "
             "when a field could not be determined — the next agent needs to know it "
             "is a known unknown rather than untried.",
    )
    r.add_argument("--note", help="one line on what happened, for the next agent")
    r.add_argument("--evidence", help="where these values came from")
    r.add_argument(
        "--input", action="append",
        help="repo-relative path this spec was derived from. Fingerprinted, so the "
             "next agent decides whether to refresh by comparing rather than judging. "
             "Repeatable.",
    )
    r.add_argument(
        "--force", action="store_true",
        help="overwrite asserted values with inferred ones",
    )
    r.set_defaults(func=cmd_refresh)

    p = sub.add_parser("whoami", help="show this agent's inferred identity")
    p.add_argument(
        "--no-input", action="store_true",
        help="never stop to ask — skips the offer to copy to your clipboard.",
    )
    p.add_argument(
        "--env", action="store_true",
        help="print the secrets another environment needs — SWITCHBOARD_KEY and "
             "SWITCHBOARD_TOKEN — as NAME=value lines, ready to paste into its env "
             "file or secret store. On a terminal it offers to put them on your "
             "clipboard. The hub URL and workspace are deliberately not included: "
             "they live in the committed .mcp.json, so a checkout already has them. "
             "Prints secrets for the same reason --show-key does.",
    )
    p.add_argument(
        "--no-repo", action="store_true",
        help="with --env, also print SWITCHBOARD_URL and SWITCHBOARD_WORKSPACE — for "
             "an environment that has no checkout to read them from.",
    )
    p.add_argument(
        "--show-key", action="store_true",
        help="print this repo's key, and the command that hands it to a "
             "teammate. Reads .claude/settings.local.json, so it works from a plain "
             "shell where nothing is exported. Prints a secret — it goes to stdout "
             "on purpose so it can be piped, but mind your scrollback.",
    )
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser(
        "keygen",
        help="generate a key — encryption, never sent to the hub",
        description="Generate a key for end-to-end encryption. This key "
                    "never reaches the hub, which is what makes the hub unable to "
                    "read the workspace — and unable to help you recover it. Not to "
                    "be confused with a workspace token (`register-token`), which is sent "
                    "on every request and is what grants access to a workspace.",
    )
    p.add_argument("--as-invite", action="store_true",
                   help="emit the pair as an invite string instead — a room on this "
                        "hub that nobody is in yet, ready to hand to the peers you "
                        "want in it. A side channel is a minted room, so minting "
                        "one produces the same artifact as being handed one.")
    p.add_argument("--note", help="one line for whoever pastes it: which room, and why")
    p.add_argument("--link", metavar="PAGE", nargs="?", const=PUBLISHED_PAGE_URL,
                   help="emit a URL onto a viewer page instead of the bare "
                        f"string, for someone with a browser and no checkout. "
                        f"With no PAGE, the published viewer "
                        f"({PUBLISHED_PAGE_URL}) — served from this repo with "
                        "no build step, so what runs in the recipient's "
                        "browser is diffable against the commit. The invite "
                        "rides in the fragment, which is never sent to a "
                        "server, but the page itself reads the key: name "
                        "another PAGE only if you control it or have read it.")
    p.add_argument("--no-input", action="store_true", help="never stop to ask")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser(
        "invite",
        help="one string carrying everything a peer needs to join this room",
        description="Joining takes five facts — hub, token, workspace, key, identity — "
                    "and each must match a peer's independently. Getting any wrong fails "
                    "SILENTLY: every call succeeds and you are simply somewhere else, "
                    "alone, with an inbox that looks exactly like a quiet one. This "
                    "collapses five chances to differ into one.",
    )
    p.add_argument("--note", help="one line for whoever pastes it: which room, and why")
    p.add_argument("--no-key", action="store_true",
                   help="omit the key (the peer must already hold it)")
    p.add_argument("--no-token", action="store_true", help="omit the hub token")
    p.add_argument("--link", metavar="PAGE", nargs="?", const=PUBLISHED_PAGE_URL,
                   help="emit a URL onto a viewer page instead of the bare "
                        f"string, for someone with a browser and no checkout. "
                        f"With no PAGE, the published viewer "
                        f"({PUBLISHED_PAGE_URL}) — served from this repo with "
                        "no build step, so what runs in the recipient's "
                        "browser is diffable against the commit. The invite "
                        "rides in the fragment, which is never sent to a "
                        "server, but the page itself reads the key: name "
                        "another PAGE only if you control it or have read it.")
    p.add_argument("--no-input", action="store_true", help="never stop to ask")
    p.set_defaults(func=cmd_invite)

    p = sub.add_parser(
        "join",
        help="consume an invite, and prove the room rather than assuming it",
        description="Prints what to export, then writes a sealed value to that room and "
                    "reads it back. That round trip is the only check that distinguishes "
                    "'same room' from 'same roster' — two agents listed together on one "
                    "hub, on different keys, see each other and can exchange nothing.",
    )
    # SUPPRESS, not None: this dest is shared with the global `--invite`, and a
    # plain default would overwrite it with nothing whenever the positional is
    # absent — so `switchboard --invite <blob> join` would lose the invite. See
    # `_accept_global_flags_after_subcommand`, which relies on the same trick.
    p.add_argument("invite", nargs="?", default=argparse.SUPPRESS,
                   help="the string from `switchboard invite`")
    p.add_argument("--no-verify", action="store_true",
                   help="print the settings without touching the hub")
    p.add_argument("--save", metavar="NAME",
                   help="what to call this room in the rooms record it prints "
                        "(default: 'invited')")
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("health", help="check the hub is reachable")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("stats", help="hub-wide counts")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser(
        "announce", aliases=["register"],
        help="announce this agent to the hub",
        description="Tell the hub this agent is here. Nothing is registered in "
                    "any lasting sense: the record is self-asserted, expires in "
                    "two minutes unless a heartbeat renews it, and no part of it "
                    "is validated. Peers witness it; nothing vouches for it. The "
                    "old name `register` still works.",
    )
    p.add_argument("--name")
    p.add_argument("--kind", choices=["local", "cloud", "ci", "unknown"])
    p.add_argument("--task", help="what this agent is working on")
    p.add_argument("-c", "--channel", action="append", help="subscribe (repeatable)")
    p.add_argument("--ttl", type=float)
    p.add_argument(
        "--back-in", type=float, metavar="SECONDS",
        help="how long until you expect to be back. Presence still lapses on its own "
             "TTL; this keeps you listed as `away` afterwards so a peer arriving into "
             "an otherwise empty roster can tell 'nobody is coming' from 'mid-turn'. "
             "The single cheapest thing you can do for a peer you have not met yet.",
    )
    p.set_defaults(func=cmd_register)

    p = sub.add_parser(
        "arrive",
        help="the first thing to run in a room: read every durable surface, and leave one",
        description="Announce, read the roster, the coord/ board and your inbox in one "
                    "go, and write what you came to do somewhere that outlives your "
                    "turn. Run this before concluding anything about a room. Checking "
                    "one surface and finding it empty is not the same finding as the "
                    "room being empty — two agents proved that on 2026-08-17, one "
                    "reading the roster and one reading its inbox, while a board entry "
                    "neither of them looked at sat in the room between them.",
    )
    p.add_argument("intent", nargs="?", help="one line on what you came to do")
    p.add_argument(
        "--back-in", type=float, metavar="SECONDS",
        help="how long until you expect to be back, so you stay listed as `away`",
    )
    p.add_argument("-c", "--channel", action="append", help="subscribe (repeatable)")
    p.set_defaults(func=cmd_arrive)

    p = sub.add_parser(
        "rendezvous",
        help="find a peer you have not met yet, and leave a note if you do not",
        description="First contact, which every other timing signal here assumes has "
                    "already happened: a forecast is built from history with a peer and "
                    "rides on a message, so none of it helps before the first exchange. "
                    "This announces you, reads notes other agents left, looks on an "
                    "escalating backoff, writes your own note, and tells you the shared "
                    "slot to come back at — derived from the workspace, so your peer "
                    "derives the same one without either of you saying anything.",
    )
    p.add_argument(
        "topic", nargs="?",
        help="what the meeting is about; both sides must use the same string. "
             f"Omit it to use the reserved `{rendezvous.OPEN_TOPIC}` topic, which is "
             "how an agent with capacity and an agent with a task meet without "
             "having agreed anything — every holder of the key can see it, so meet "
             "there and move the work somewhere narrower.",
    )
    p.add_argument("--want", help="one line on what you need, for whoever finds your note")
    p.add_argument(
        "--offer", metavar="WHAT",
        help="you have capacity rather than a task: one line on what you can do. "
             "Offers match seekers and never other offers, so a room full of idle "
             "helpers does not report itself as a meeting.",
    )
    p.add_argument(
        "--wait", type=float, metavar="SECONDS",
        help="how long this invocation spends looking (default 60). Bounded on purpose: "
             "a turn-based agent cannot hold a socket for an hour, and the note plus the "
             "shared slot are what cover the rest.",
    )
    p.add_argument(
        "--until", type=float, metavar="SECONDS",
        help="how long your note should claim you are still looking (default 30m). "
             "Past it, peers treat it as litter rather than sending someone to wait on you.",
    )
    p.add_argument(
        "--back-in", type=float, metavar="SECONDS",
        help="how long until you expect to be back, so you stay listed as `away`",
    )
    p.add_argument("--task", help="what this agent is working on")
    p.add_argument("-c", "--channel", action="append", help="subscribe (repeatable)")
    p.set_defaults(func=cmd_rendezvous)

    p = sub.add_parser("agents", help="who else is awake")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("claim", help="take an exclusive lease on a resource")
    p.add_argument("resource")
    p.add_argument("-m", "--note", help="why you want it")
    p.add_argument("--ttl", type=float, help="seconds (default 900)")
    p.add_argument(
        "--declare", action="store_true",
        help="also record a standing claim on the board, which outlives this "
             "lease (a day, unless you release it). Use it when the resource is "
             "yours across turns rather than for the next few minutes — a lease "
             "cannot say that, because renewal is a heartbeat side effect and it "
             "lapses the moment you stop. Anyone claiming it is warned, not "
             "blocked.",
    )
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release", help="drop a lease")
    p.add_argument("resource")
    p.add_argument("--force", action="store_true", help="release even if held by another agent")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("renew", help="extend one lease, leaving your others alone")
    p.add_argument("resource")
    p.add_argument("--ttl", type=float, help="seconds to extend it by")
    p.set_defaults(func=cmd_renew)

    p = sub.add_parser(
        "leave", help="drop off the roster deliberately",
        description="Retire an agent from the roster. Yourself by default, or "
                    "an id `agents` is showing — which is how you clear a ghost "
                    "of your own, left behind when a branch checkout re-derived "
                    "your identity and stranded the old one with a return time "
                    "it will never honour.",
    )
    p.add_argument("agent", nargs="?",
                   help="an agent id as `agents` prints it (default: yourself)")
    p.set_defaults(func=cmd_leave)

    p = sub.add_parser("claims", help="list live leases")
    p.add_argument("--holder", help="filter by holding agent")
    p.set_defaults(func=cmd_claims)

    p = sub.add_parser("say", help="post to a channel")
    p.add_argument("channel")
    p.add_argument("message", nargs="*", help="text, or - to read stdin")
    p.add_argument("--type", default="note")
    p.add_argument("--thread")
    p.add_argument("--ttl", type=float)
    p.add_argument("--json-body", action="store_true", help="parse the message as JSON")
    _add_timing_args(p)
    p.set_defaults(func=cmd_say)

    p = sub.add_parser("dm", help="message one agent directly")
    p.add_argument("to")
    p.add_argument("message", nargs="*", help="text, or - to read stdin")
    p.add_argument("--type", default="note")
    p.add_argument("--thread")
    p.add_argument("--ttl", type=float)
    p.add_argument("--json-body", action="store_true")
    _add_timing_args(p)
    p.set_defaults(func=cmd_dm)

    p = sub.add_parser(
        "whisper",
        help="message one agent so only they can read it, even on this workspace's key")
    p.add_argument("to")
    p.add_argument("message", nargs="*", help="text, or - to read stdin")
    p.add_argument("--type", default=WHISPER_TYPE)
    p.add_argument("--ttl", type=float)
    p.add_argument("--json-body", action="store_true")
    _add_timing_args(p)
    p.set_defaults(func=cmd_whisper)

    p = sub.add_parser("inbox", help="drain new messages for this agent")
    p.add_argument("-c", "--channel", action="append", help="override subscriptions")
    p.add_argument("--wait", type=float, default=0.0, help="long-poll up to N seconds")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--peek", action="store_true", help="do not advance the read cursor")
    p.add_argument("--include-own", action="store_true")
    _add_timing_args(p)
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser(
        "listen",
        help="park until a message arrives, then exit so your runner wakes you",
        description="Park on this agent's inbox and exit the moment something "
                    "arrives, with the message on stdout. Meant to be run as a "
                    "background process by a runner that re-invokes a session "
                    "when one exits — that exit is the wake. It peeks rather "
                    "than drains, so the woken session still calls `inbox` to "
                    "take delivery. Exit 0 a message arrived, 2 the deadline "
                    "passed, 1 it never watched anything.",
    )
    p.add_argument("-c", "--channel", action="append",
                   help="wake on these channels instead of this agent's own inbox")
    p.add_argument(
        "--until", metavar="WHEN",
        help="when to give up and come back empty: an ISO-8601 time, +SECONDS, "
             "or forecast:p50 / forecast:p95 to park until the moment this "
             "agent's own timing model says it would next have looked anyway. "
             "Without one it parks indefinitely, which is a promise to be "
             "reachable that nothing keeps.",
    )
    p.add_argument("--execution-class", metavar="LABEL",
                   help="with --until forecast:…, the kind of work ahead")
    p.add_argument("--effort", choices=["low", "medium", "high"],
                   help="with --until forecast:…, the rough size of it")
    p.add_argument("--ttl", type=float,
                   help=f"heartbeat TTL in seconds (default {int(_LISTEN_TTL)})")
    p.add_argument("--max-fails", type=int, default=5,
                   help="consecutive hub errors tolerated before giving up")
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("watch", help="follow messages until interrupted")
    p.add_argument("-c", "--channel", action="append")
    p.add_argument("--limit", type=int, default=100)
    _add_timing_args(p)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("history", help="recent messages on a channel (cursor untouched)")
    p.add_argument("channel")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("channels", help="list active channels")
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("checkin", help="heartbeat + renew leases + drain inbox")
    p.add_argument("--task")
    p.add_argument("--ttl", type=float)
    p.add_argument("--wait", type=float, default=0.0)
    p.add_argument("--limit", type=int, default=100)
    _add_timing_args(p)
    p.add_argument(
        "--back-in", type=float, metavar="SECONDS",
        help="how long until you expect to be back. Presence still lapses on its own "
             "TTL; this keeps you listed as `away` afterwards so a peer arriving into "
             "an otherwise empty roster can tell 'nobody is coming' from 'mid-turn'. "
             "The single cheapest thing you can do for a peer you have not met yet.",
    )
    p.set_defaults(func=cmd_checkin)

    p = sub.add_parser("timing", help="this agent's own timing model (local, never shared)")
    _add_timing_args(p)
    p.set_defaults(func=cmd_timing)

    p = sub.add_parser(
        "drill",
        help="launch a few agents at one task and report what the hub saw",
        description="Start N worker agents, brief them through the hub, and "
                    "observe them only through presence, messages, leases and "
                    "the blackboard. Exits non-zero if any worker failed or "
                    "never reported.",
    )
    p.add_argument("task", help="what the workers should do")
    p.add_argument("-n", "--agents", type=int, default=drill.DEFAULT_AGENTS,
                   help=f"how many workers (default {drill.DEFAULT_AGENTS})")
    p.add_argument("--worker", dest="worker_kind", default="auto",
                   choices=["auto", "claude", "claude-mcp", "builtin", "custom"],
                   help="auto: real `claude` sessions when available, else the "
                        "scripted builtin worker. claude-mcp: real sessions that "
                        "reach the hub through the MCP server instead of the CLI")
    p.add_argument("--worker-cmd", help="command for --worker custom; "
                                        "{slot} and {run_id} are substituted")
    p.add_argument("--model", help="model for --worker claude and claude-mcp")
    p.add_argument("--verify", help="shell command each worker runs and reports on, "
                                    "e.g. 'pytest -q'")
    p.add_argument("--timeout", type=float, default=drill.DEFAULT_TIMEOUT,
                   help=f"seconds before unreported workers are terminated "
                        f"(default {drill.DEFAULT_TIMEOUT:.0f})")
    p.add_argument("--dir", help="working directory for the workers")
    p.add_argument("--out", help="also write the report JSON to this path")
    p.set_defaults(func=cmd_drill)

    p = sub.add_parser("board", help="shared key/value scratch space")
    bsub = p.add_subparsers(dest="board_action", required=True)
    # Named "board_key" (displayed as "key" via metavar): the implicit dest
    # from a positional literally named "key" would collide with the global
    # --key (workspace encryption key) on the same Namespace, letting a board
    # entry name silently overwrite/be mistaken for the encryption key — see
    # issue #16. argparse has no dest= override for positionals, so the
    # argument itself has to be renamed.
    b = bsub.add_parser("set")
    b.add_argument("board_key", metavar="key")
    b.add_argument("value")
    b.add_argument("--ttl", type=float)
    b.add_argument("--if-revision", type=int, help="optimistic concurrency; 0 means 'if absent'")
    b.add_argument("--json-body", action="store_true")
    b = bsub.add_parser("get")
    b.add_argument("board_key", metavar="key")
    b = bsub.add_parser("list")
    b.add_argument("--prefix")
    b = bsub.add_parser("delete")
    b.add_argument("board_key", metavar="key")
    p.set_defaults(func=cmd_board)

    _accept_global_flags_after_subcommand(parser)
    return parser


def _escape_dash_leading_positionals(
    parser: argparse.ArgumentParser, argv: Sequence[str],
) -> list[str]:
    """Insert ``--`` before a positional that argparse would read as a flag.

    The hub mints agent ids as base64url, whose alphabet includes ``-``, so
    about one id in sixty-six begins with one. `switchboard dm -yLAoQ63… hi`
    then dies on `unrecognized arguments`, and the id came from `switchboard
    agents` — the tool refusing a value it generated and told you to use. It
    surfaced as a 1-in-66 CI failure long before anyone would have reported it
    as a bug, because a peer you cannot address looks like a peer who is not
    there.

    Deterministic rather than clever: walk the resolved subcommand's own
    option strings, skip the values of flags that take one, and escape the
    first token that starts with ``-`` and is not an option this subcommand
    knows. So `claim x -m "-a dashed note"` is untouched (`-m` is real, and
    the dashed text is its value), while `board get -abc` is escaped.

    Writing `--` by hand still works, and is still what a shell user should
    reach for. This only means they no longer have to know that.
    """
    args = list(argv)
    if "--" in args:
        return args

    def subparsers_of(p: argparse.ArgumentParser) -> dict[str, Any]:
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action.choices
        return {}

    # Walk down to the subcommand actually being run — stepping over the global
    # flags that may precede it (`--json claim …`) and over their values, since
    # a value is allowed to spell a subcommand name (`-w claim`).
    index, current, depth = 0, parser, 0
    while index < len(args):
        choices = subparsers_of(current)
        token = args[index]
        if token in choices:
            current, depth = choices[token], depth + 1
            index += 1
            continue
        if token in current._option_string_actions:
            action = current._option_string_actions[token]
            index += 2 if action.nargs != 0 else 1
            continue
        if token.startswith("--") and "=" in token:
            index += 1
            continue
        break
    if not depth:
        return args  # no subcommand yet; nothing positional to protect

    options = set(current._option_string_actions)
    while index < len(args):
        token = args[index]
        if token in options:
            action = current._option_string_actions[token]
            index += 2 if action.nargs != 0 else 1
            continue
        if token.startswith("--") and "=" in token and token.split("=", 1)[0] in options:
            index += 1
            continue
        if token.startswith("-") and len(token) > 1:
            return args[:index] + ["--"] + args[index:]
        index += 1
    return args


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    args, extras = parser.parse_known_args(_escape_dash_leading_positionals(parser, argv))
    if extras:
        # `say general --thread plan hello` used to die on "unrecognized
        # arguments: hello" — argparse cannot place a `nargs="*"` positional
        # once an option has intervened, so the message an agent typed came
        # back as the error. Worse than confusing: the shape of the complaint
        # blames the message rather than the ordering, and the post silently
        # does not happen.
        #
        # Deliberately narrow, in the spirit of the escaping above: only a
        # command that actually takes a free-text `message`, and only tokens
        # that are not trying to be flags. A mistyped `--tread` still errors
        # rather than being posted as somebody's words.
        message = getattr(args, "message", None)
        if isinstance(message, list) and all(not e.startswith("-") for e in extras):
            args.message = [*message, *extras]
        else:
            parser.error("unrecognized arguments: " + " ".join(extras))
    try:
        return args.func(args)
    except LeaseHeld as exc:
        print(f"conflict: {exc}", file=sys.stderr)
        return EXIT_CONFLICT
    except CryptoError as exc:
        print(f"encryption error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SwitchboardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if getattr(exc, "status", None) == 401:
            # The one error where the next action depends entirely on facts the
            # message cannot carry. A stale `SWITCHBOARD_TOKEN` left over from a
            # throwaway hub produces this against a real one, and reads as the
            # tool being broken rather than as the shell being dirty.
            print(_tried(_for_report(args)), file=sys.stderr)
            print("  Export the hub's token as SWITCHBOARD_TOKEN, pass --token, "
                  "or use --invite, which carries it.", file=sys.stderr)
        return EXIT_ERROR
    except httpx.HTTPError as exc:
        # From inside httpx rather than at a boundary, so without this it
        # escapes as forty frames of traceback with the one useful fact — which
        # URL — nowhere in it. See issue #88.
        print(f"error: could not reach the hub ({exc.__class__.__name__}: {exc})",
              file=sys.stderr)
        print(_tried(_for_report(args)), file=sys.stderr)
        print("  Set SWITCHBOARD_URL, or check the hub is running.", file=sys.stderr)
        return EXIT_ERROR
    except invite.InviteError as exc:
        # Raised by `--invite` on any command, from inside `_make_config` — so
        # every command gets the parse error as a sentence rather than the
        # traceback each would otherwise have to catch for itself.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_OK
    except OSError as exc:
        print(f"cannot reach hub: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
