"""Drills: launch a few agents at a task and watch them coordinate.

A drill is a test *of the hub as agents actually use it*, run end to end.
The coordinator starts N worker agents, hands them one task through
Switchboard, and then — this is the part that matters — learns everything
else through the hub too. It does not read the workers' stdout to find out
whether they started, whether they got the brief, or what they concluded.
It reads presence, messages, leases and the blackboard, exactly as a peer
agent would. Anything the hub cannot tell it, the report records as
unknown, because that is the honest answer for a coordination tool: if the
only way to know a worker is alive is to look at its terminal, the hub
failed at its one job.

Two kinds of worker exist, and the difference is deliberate:

* **claude** — a real `claude -p` session, given the protocol in its
  prompt. This is the real test, and it is nondeterministic, needs a
  binary on PATH and costs tokens.
* **builtin** — ``switchboard drill worker``, a scripted agent in this
  module that walks the same protocol. It exercises the hub identically
  and finishes in under a second, so the drill itself is testable in CI.

Both speak the same protocol, so the coordinator has one implementation
and the report has one shape.

The protocol, which is also what the claude prompt says:

1. register (announce) with the hub
2. say hello on the drill channel
3. claim ``drill/<run>/<slot>`` — a lease nobody else should get
4. do the task, running the verify command if one was given
5. write the result to the blackboard at ``drill/<run>/results/<agent>``
6. say done on the channel

Steps 2, 3, 5 and 6 are each observable from the outside, which is what
turns "did it work" into a measurement rather than an impression.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .client import Client, SwitchboardError

#: How long a worker's presence, leases and result live. Generous relative
#: to a drill: a run that overruns should leave its evidence behind long
#: enough to be looked at, and everything here expires on its own anyway.
RESULT_TTL = 3600.0
LEASE_TTL = 900.0

DEFAULT_AGENTS = 3
DEFAULT_TIMEOUT = 300.0
POLL_INTERVAL = 1.0
FIRST_POLL_INTERVAL = 0.05


def channel_for(run_id: str) -> str:
    return f"drill/{run_id}"


def brief_key(run_id: str) -> str:
    return f"drill/{run_id}/brief"


def result_prefix(run_id: str) -> str:
    return f"drill/{run_id}/results/"


def report_key(run_id: str) -> str:
    return f"drill/{run_id}/report"


def new_run_id() -> str:
    return f"{int(time.time())}-{secrets.token_hex(3)}"


# --- the brief --------------------------------------------------------------


@dataclass
class Brief:
    """What every worker is told, and the one copy of it on the hub.

    Deliberately a hub object rather than an argv string: a worker that
    cannot read this from the blackboard is a worker that cannot
    coordinate, and the drill should fail on that rather than paper over
    it by having passed the task in the environment. The environment does
    carry it too, for the builtin worker's convenience — but the claude
    prompt tells the agent to read the board, so the real workers prove
    the read path.
    """

    run_id: str
    task: str
    verify: str | None = None
    deadline_s: float = DEFAULT_TIMEOUT
    #: How many workers this run has. On the brief because a worker needs it
    #: to know when its peers have all arrived — see the builtin worker's
    #: barrier, which is what makes the lease contention real rather than
    #: three agents taking turns at an uncontended key.
    workers: int = DEFAULT_AGENTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "verify": self.verify,
            "deadline_s": self.deadline_s,
            "workers": self.workers,
            "channel": channel_for(self.run_id),
            "result_prefix": result_prefix(self.run_id),
        }


WORKER_PROMPT = """\
You are worker `{slot}` in a Switchboard coordination drill (run `{run_id}`).

Everything you need is on the hub, and everything you report goes back to
it. Use the `switchboard` CLI (aliased `swb`); it is already pointed at the
right hub and workspace by the environment.

Protocol — follow it in order:

1. `switchboard announce --task "drill {run_id} worker {slot}"`
2. `switchboard board get {brief_key}` — this is your brief. Read it.
3. `switchboard say {channel} "worker {slot} up"`
4. `switchboard claim drill/{run_id}/{slot}` — your own slot, so it should
   succeed. If it does not, say so on the channel and keep going.
5. Do the task described in the brief.
6. `switchboard board set drill/{run_id}/results/$AGENT --json-body '<json>'`
   where `$AGENT` is your agent id (`switchboard whoami --json`) and the
   JSON is:
   {{"ok": true|false, "slot": "{slot}", "summary": "one line",
     "checks": [{{"name": "...", "ok": true|false, "detail": "..."}}]}}
   `ok` is your honest verdict on whether the task succeeded. Do not report
   true because the drill would look better; a drill that always passes
   measures nothing.
7. `switchboard say {channel} "worker {slot} done"`

Then stop. Do not open a pull request, do not commit, and do not touch any
file outside the working directory you were started in.

The task:

{task}
"""


def worker_prompt(brief: Brief, slot: str) -> str:
    return WORKER_PROMPT.format(
        slot=slot,
        run_id=brief.run_id,
        channel=channel_for(brief.run_id),
        brief_key=brief_key(brief.run_id),
        task=brief.task,
    )


# --- launching --------------------------------------------------------------


@dataclass
class Worker:
    slot: str
    agent_id: str
    argv: list[str]
    proc: subprocess.Popen | None = None
    launched_at: float = 0.0
    exited_at: float | None = None
    exit_code: int | None = None
    #: Hub-observed milestones, all as absolute epoch seconds so the report
    #: can express them as offsets from launch without the observer having
    #: to remember when it noticed.
    announced_at: float | None = None
    first_message_at: float | None = None
    done_message_at: float | None = None
    result_at: float | None = None
    result: dict[str, Any] | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    leases: list[str] = field(default_factory=list)

    def telemetry(self) -> dict[str, Any]:
        def since(ts: float | None) -> float | None:
            return None if ts is None else round(ts - self.launched_at, 3)

        result = self.result or {}
        checks = result.get("checks") if isinstance(result.get("checks"), list) else []
        return {
            "slot": self.slot,
            "agent_id": self.agent_id,
            "exit_code": self.exit_code,
            # Every latency is measured from launch and observed through the
            # hub. `announced_s` is "how long until anyone else could tell
            # this agent existed", which is the number a coordination hub is
            # actually judged on.
            "announced_s": since(self.announced_at),
            "first_message_s": since(self.first_message_at),
            "done_message_s": since(self.done_message_at),
            "result_s": since(self.result_at),
            "wall_s": since(self.exited_at),
            "messages": len(self.messages),
            "leases": sorted(set(self.leases)),
            "reported": self.result is not None,
            "ok": bool(result.get("ok")) if self.result is not None else None,
            "summary": result.get("summary"),
            "checks": checks,
        }

    def verdict(self) -> str:
        """One word for what became of this worker.

        `silent` and `failed` are kept apart on purpose. A worker that ran
        and honestly reported failure tested the task; a worker that never
        reported tested the hub, and found it — or the launch — wanting.
        Collapsing them into "not ok" would hide which one happened.
        """
        if self.result is None:
            return "silent"
        return "passed" if self.result.get("ok") else "failed"


def _worker_env(brief: Brief, worker_agent_id: str, base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    env["SWITCHBOARD_AGENT_ID"] = worker_agent_id
    env["SWITCHBOARD_DRILL_RUN"] = brief.run_id
    env["SWITCHBOARD_DRILL_SLOT"] = worker_agent_id.rsplit("-", 1)[-1]
    env["SWITCHBOARD_DRILL_TASK"] = brief.task
    if brief.verify:
        env["SWITCHBOARD_DRILL_VERIFY"] = brief.verify
    else:
        env.pop("SWITCHBOARD_DRILL_VERIFY", None)
    return env


def build_workers(
    brief: Brief,
    *,
    count: int,
    kind: str,
    worker_cmd: str | None = None,
    model: str | None = None,
) -> list[Worker]:
    """Decide what each worker will be, without starting anything yet.

    Split from `launch` so the shape of a run can be inspected — and
    tested — without spending a subprocess on it.
    """
    workers: list[Worker] = []
    for index in range(count):
        slot = f"w{index + 1}"
        agent_id = f"drill-{brief.run_id}-{slot}"
        if kind == "builtin":
            argv = [sys.executable, "-m", "switchboard.drill", "worker"]
        elif kind == "claude":
            argv = ["claude", "-p", worker_prompt(brief, slot)]
            if model:
                argv += ["--model", model]
        elif kind == "custom":
            if not worker_cmd:
                raise ValueError("--worker-cmd is required with --worker custom")
            argv = shlex.split(worker_cmd.format(slot=slot, run_id=brief.run_id))
        else:  # pragma: no cover - argparse constrains the choices
            raise ValueError(f"unknown worker kind: {kind}")
        workers.append(Worker(slot=slot, agent_id=agent_id, argv=argv))
    return workers


def launch(workers: Sequence[Worker], brief: Brief, *, cwd: str | None = None,
           env: dict[str, str] | None = None, capture: bool = True) -> None:
    base = dict(env or os.environ)
    for worker in workers:
        worker.launched_at = time.time()
        worker.proc = subprocess.Popen(  # noqa: S603 - argv is built above, not shell
            worker.argv,
            cwd=cwd,
            env=_worker_env(brief, worker.agent_id, base),
            stdout=subprocess.DEVNULL if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True,
        )


# --- observing --------------------------------------------------------------


class Observer:
    """Watches a drill through the hub and nothing else.

    Every method here reads one of the four primitives. The deliberate
    omission is worker stdout: it is discarded (or kept only for a failure
    postmortem), so the report cannot accidentally be built from a channel
    the agents themselves do not have.
    """

    def __init__(self, hub: Client, brief: Brief, workers: Sequence[Worker]):
        self.hub = hub
        self.brief = brief
        self.workers = list(workers)
        # Keyed by the id the *hub* uses, which is not the id we assigned: on
        # a sealed workspace every agent id is blinded before it leaves the
        # client, so presence rows, message senders and lease holders all come
        # back as opaque tokens. Blinding our side once here is what lets the
        # rest of this class compare them at all.
        self.by_agent = {self.hub_id(w.agent_id): w for w in self.workers}
        self.hub_polls = 0
        self.channel_messages: list[dict[str, Any]] = []
        self.errors: list[str] = []

    def hub_id(self, local_agent_id: str) -> str:
        cipher = self.hub.cipher
        return cipher.blind(local_agent_id, "agent") if cipher else local_agent_id

    def _attribute(self, agent_id: str) -> Worker | None:
        """Map a hub-visible agent id back to a worker slot.

        Exact match first. A claude worker inherits the id we set in its
        environment, but a custom worker may mint its own, so fall back to
        the run id embedded in the slot lease/board key it used.
        """
        worker = self.by_agent.get(agent_id)
        if worker is not None:
            return worker
        for candidate in self.workers:
            if candidate.slot in agent_id and self.brief.run_id in agent_id:
                return candidate
        return None

    def poll(self) -> None:
        self.hub_polls += 1
        now = time.time()
        self._poll_presence(now)
        self._poll_messages(now)
        self._poll_leases()
        self._poll_results(now)

    def _poll_presence(self, now: float) -> None:
        try:
            agents = self.hub.agents()
        except SwitchboardError as exc:
            self.errors.append(f"agents: {exc}")
            return
        for agent in agents:
            worker = self._attribute(agent.get("agent_id", ""))
            if worker is not None and worker.announced_at is None:
                worker.announced_at = now

    def _poll_messages(self, now: float) -> None:
        try:
            messages = self.hub.inbox(channels=[channel_for(self.brief.run_id)], limit=200)
        except SwitchboardError as exc:
            self.errors.append(f"inbox: {exc}")
            return
        for message in messages:
            self.channel_messages.append(message)
            worker = self._attribute(message.get("from", ""))
            if worker is None:
                continue
            worker.messages.append(message)
            if worker.first_message_at is None:
                worker.first_message_at = now
            # A worker is present the moment it speaks, whether or not the
            # presence poll happened to catch it first.
            if worker.announced_at is None:
                worker.announced_at = now
            if "done" in str(message.get("body", "")).lower():
                worker.done_message_at = now

    def _poll_leases(self) -> None:
        try:
            leases = self.hub.leases()
        except SwitchboardError as exc:
            self.errors.append(f"leases: {exc}")
            return
        for lease in leases:
            worker = self._attribute(lease.get("holder", ""))
            if worker is not None:
                # On a sealed workspace this is the blinded token, not the
                # readable key — resources are compared by the hub, never
                # read by it. The count and the holder are still true, which
                # is what the drill is measuring.
                worker.leases.append(lease["resource"])

    def _poll_results(self, now: float) -> None:
        """Fetch each outstanding worker's result by its exact key.

        Deliberately not a prefix listing. Board keys are blinded before
        they leave the client when the workspace has a key, so a prefix
        matches nothing on a sealed workspace — the drill would report
        every worker silent precisely on the configuration the project
        treats as the default. An exact get blinds the same way the write
        did, so it works either way, and the worker's key is known in
        advance because the coordinator assigned its agent id.
        """
        for worker in self.workers:
            if worker.result is not None:
                continue
            key = f"{result_prefix(self.brief.run_id)}{worker.agent_id}"
            try:
                value = self.hub.board_get(key)
            except SwitchboardError as exc:
                self.errors.append(f"board {key}: {exc}")
                continue
            if value is None:
                continue
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = {"ok": False, "summary": value}
            if not isinstance(value, dict):
                value = {"ok": False, "summary": str(value)}
            worker.result = value
            worker.result_at = now

    def settled(self) -> bool:
        """True once nothing further can be learned by waiting.

        A worker whose process has exited will never report again, so a run
        where every worker has either reported or exited is finished — the
        timeout exists for the case where a process hangs, not as the
        normal path.
        """
        for worker in self.workers:
            if worker.result is not None:
                continue
            if worker.proc is not None and worker.proc.poll() is None:
                return False
            if worker.proc is None:
                return False
        return True


def reap(workers: Sequence[Worker]) -> None:
    now = time.time()
    for worker in workers:
        if worker.proc is None or worker.exit_code is not None:
            continue
        code = worker.proc.poll()
        if code is not None:
            worker.exit_code = code
            worker.exited_at = worker.exited_at or now


def terminate(workers: Sequence[Worker]) -> None:
    for worker in workers:
        if worker.proc is None or worker.proc.poll() is not None:
            continue
        worker.proc.terminate()
        try:
            worker.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.proc.kill()
    reap(workers)


# --- the report -------------------------------------------------------------


def build_report(brief: Brief, observer: Observer, *, started_at: float,
                 timed_out: bool) -> dict[str, Any]:
    workers = observer.workers
    verdicts = [w.verdict() for w in workers]
    passed = verdicts.count("passed")
    failed = verdicts.count("failed")
    silent = verdicts.count("silent")
    latencies = [w.telemetry()["announced_s"] for w in workers]
    observed = [x for x in latencies if x is not None]
    return {
        "run_id": brief.run_id,
        "task": brief.task,
        "verify": brief.verify,
        "started_at": started_at,
        "wall_s": round(time.time() - started_at, 3),
        "timed_out": timed_out,
        # The verdict is the conjunction, not the majority: a drill exists
        # to notice the one worker that never spoke.
        "ok": failed == 0 and silent == 0 and not timed_out and passed == len(workers),
        "counts": {
            "workers": len(workers),
            "passed": passed,
            "failed": failed,
            "silent": silent,
        },
        "telemetry": {
            "hub_polls": observer.hub_polls,
            "channel_messages": len(observer.channel_messages),
            "observer_errors": observer.errors,
            "announce_latency_s": {
                "min": min(observed) if observed else None,
                "max": max(observed) if observed else None,
                "mean": round(sum(observed) / len(observed), 3) if observed else None,
                "observed": len(observed),
            },
        },
        "workers": [w.telemetry() for w in workers],
    }


def run_drill(
    hub: Client,
    *,
    task: str,
    count: int = DEFAULT_AGENTS,
    kind: str = "builtin",
    verify: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    worker_cmd: str | None = None,
    model: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    run_id: str | None = None,
    on_event: Any = None,
) -> dict[str, Any]:
    """Run one drill to completion and return its report.

    The coordinator registers itself first. It is a participant, not an
    outside observer, and a hub where the observer is exempt from the
    protocol is not the hub the workers are being tested against.
    """
    brief = Brief(run_id=run_id or new_run_id(), task=task, verify=verify,
                  deadline_s=timeout, workers=count)
    channel = channel_for(brief.run_id)
    started_at = time.time()

    def emit(text: str) -> None:
        if on_event is not None:
            on_event(text)

    hub.register(name=f"drill-coordinator:{brief.run_id}", kind="drill",
                 task=f"drill {brief.run_id}", channels=[channel])
    hub.board_set(brief_key(brief.run_id), brief.to_dict(), ttl=RESULT_TTL)
    hub.post(channel, f"drill {brief.run_id} starting: {task}", type="note")
    # Drain our own brief so the worker telemetry is not contaminated by the
    # coordinator's own message showing up as channel traffic to attribute.
    hub.inbox(channels=[channel], limit=50)

    workers = build_workers(brief, count=count, kind=kind, worker_cmd=worker_cmd, model=model)
    emit(f"launching {len(workers)} {kind} worker(s) on {channel}")
    launch(workers, brief, cwd=cwd, env=env)

    observer = Observer(hub, brief, workers)
    deadline = started_at + timeout
    timed_out = False
    ticks = 0
    try:
        while True:
            observer.poll()
            reap(workers)
            if observer.settled():
                # One last poll: a worker can write its result and exit
                # between two polls, and giving up on it because the process
                # is gone would drop a result the hub already has.
                observer.poll()
                break
            if time.time() >= deadline:
                timed_out = True
                emit(f"timed out after {timeout:.0f}s")
                break
            # Poll fast at first, then back off to POLL_INTERVAL. The
            # announce latency is the headline number and it is usually
            # well under a second, so a flat one-second poll would report
            # every run at "1.0s" — the observer's own granularity dressed
            # up as a measurement of the hub.
            time.sleep(min(POLL_INTERVAL, FIRST_POLL_INTERVAL * (2 ** ticks)))
            ticks += 1
    finally:
        terminate(workers)

    report = build_report(brief, observer, started_at=started_at, timed_out=timed_out)
    try:
        hub.board_set(report_key(brief.run_id), report, ttl=RESULT_TTL)
        hub.post(channel, f"drill {brief.run_id} {'ok' if report['ok'] else 'FAILED'}",
                 type="note")
    except SwitchboardError as exc:  # the run still happened; say so and move on
        report["telemetry"]["observer_errors"].append(f"report publish: {exc}")
    return report


def claude_available() -> bool:
    return shutil.which("claude") is not None


# --- the builtin worker -----------------------------------------------------


def _run_verify(command: str, cwd: str | None = None) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(  # noqa: S602 - a verify command is shell by design
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return {"name": "verify", "ok": False, "detail": "timed out after 600s",
                "duration_s": round(time.time() - started, 3)}
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    return {
        "name": "verify",
        "ok": proc.returncode == 0,
        "detail": (tail[-1] if tail else f"exit {proc.returncode}")[:400],
        "duration_s": round(time.time() - started, 3),
    }


#: How long a worker waits at the barrier for its peers before giving up and
#: reporting how many it saw. A cap, not a schedule: the common case returns
#: as soon as the last peer registers.
PEER_WAIT = 20.0

#: The name every builtin worker registers under. Peers find each other by
#: this rather than by id — see `_await_peers`.
WORKER_NAME = "drill-worker:"


def _await_peers(hub: Client, expected: int) -> int:
    """Block until every worker in this run is visible on the hub.

    Without this the drill's contention probe proves nothing: worker
    processes start milliseconds apart and finish in well under a second,
    so the first one can have taken *and* given up the shared key before
    the second one asks for it, and three agents taking turns look exactly
    like three agents all winning. Waiting for the roster turns the probe
    into a genuine race — and makes presence itself something the run
    depends on, rather than something only the coordinator reads.
    """
    deadline = time.time() + PEER_WAIT
    seen = 0
    while time.time() < deadline:
        try:
            agents = hub.agents()
        except SwitchboardError:
            return seen
        # Counted by registered name, not by agent id: ids are blinded on a
        # sealed workspace and no peer can recognise another's, while the
        # name is sealed *to the shared key* and so opens for exactly the
        # agents that are really peers.
        seen = sum(1 for a in agents if str(a.get("name") or "").startswith(WORKER_NAME))
        if seen >= expected:
            return seen
        time.sleep(0.2)
    return seen


def worker_main(argv: Sequence[str] | None = None) -> int:
    """The builtin worker: walk the protocol, report honestly, exit.

    It is a real agent as far as the hub is concerned — same registration,
    same lease, same blackboard write — which is the point. What it is not
    is a model, so it can be run a hundred times in CI and always tests the
    same thing: that the coordination path works.
    """
    from .config import ClientConfig

    run_id = os.environ.get("SWITCHBOARD_DRILL_RUN")
    if not run_id:
        print("drill worker: SWITCHBOARD_DRILL_RUN is not set", file=sys.stderr)
        return 1
    slot = os.environ.get("SWITCHBOARD_DRILL_SLOT", "w?")
    channel = channel_for(run_id)
    agent_id = os.environ.get("SWITCHBOARD_AGENT_ID") or f"drill-{run_id}-{slot}"

    checks: list[dict[str, Any]] = []
    with Client(ClientConfig.from_env(), agent_id=agent_id) as hub:
        hub.register(name=f"{WORKER_NAME}{slot}", kind="drill",
                     task=f"drill {run_id} worker {slot}", channels=[channel])
        hub.post(channel, f"worker {slot} up", type="note")

        brief = hub.board_get(brief_key(run_id))
        checks.append({
            "name": "brief-readable",
            "ok": isinstance(brief, dict) and bool(brief.get("task")),
            "detail": "read the brief from the blackboard" if isinstance(brief, dict)
                      else "no brief at " + brief_key(run_id),
        })

        expected = int(brief.get("workers", 1)) if isinstance(brief, dict) else 1
        seen = _await_peers(hub, expected)
        checks.append({
            "name": "peers-visible",
            "ok": seen >= expected,
            "detail": f"saw {seen}/{expected} drill agent(s) on the hub",
        })

        try:
            hub.acquire(f"drill/{run_id}/{slot}", note=f"drill {run_id}", ttl=LEASE_TTL)
            checks.append({"name": "own-slot-lease", "ok": True, "detail": "acquired"})
        except SwitchboardError as exc:
            checks.append({"name": "own-slot-lease", "ok": False, "detail": str(exc)})

        # Every worker reaches for the same key. Exactly one should get it,
        # and the losers failing here is the expected, correct outcome — so
        # the contention probe is reported, never scored.
        contended = f"drill/{run_id}/contended"
        try:
            hub.acquire(contended, note=f"worker {slot}", ttl=LEASE_TTL)
            contention = {"name": "shared-lease", "ok": True, "detail": "won the contended key"}
        except SwitchboardError as exc:
            contention = {"name": "shared-lease", "ok": True,
                          "detail": f"yielded the contended key ({exc.status or 'conflict'})"}
        checks.append(contention)

        verify = os.environ.get("SWITCHBOARD_DRILL_VERIFY")
        if verify:
            checks.append(_run_verify(verify))

        ok = all(c["ok"] for c in checks)
        result = {
            "ok": ok,
            "slot": slot,
            "summary": f"{sum(1 for c in checks if c['ok'])}/{len(checks)} checks passed",
            "checks": checks,
        }
        hub.board_set(f"{result_prefix(run_id)}{agent_id}", result, ttl=RESULT_TTL)
        hub.post(channel, f"worker {slot} done: {result['summary']}", type="note")
        # Deliberately no deregister. Deregistering releases this worker's
        # leases immediately, which would erase the evidence the coordinator
        # is there to observe — and the project's own thesis is that a claim
        # should expire rather than be handed back. Presence and leases here
        # are short-lived by construction, so leaving them is not litter.
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "worker":
        return worker_main(args[1:])
    print("usage: python -m switchboard.drill worker", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
