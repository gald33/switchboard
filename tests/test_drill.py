"""Tests for `switchboard drill`.

The interesting half is the end-to-end one: a real hub on a real socket,
real worker subprocesses, and a coordinator that learns about them only
through the hub. In-process fakes would test the report formatter and
nothing else — the whole claim of a drill is that coordination works
across process boundaries, so the test has to cross one.

Workers are the builtin kind throughout. `claude` workers are the same
protocol driven by a model, which is neither available nor deterministic
in CI.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import closing

import pytest
import uvicorn

from switchboard import drill
from switchboard.cli import main
from switchboard.client import Client
from switchboard.config import ClientConfig, ServerConfig
from switchboard.server import create_app
from switchboard.store import Store

WS = "drill-ws"


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def hub_url(tmp_path):
    """A hub on a real port, because the workers are real processes."""
    port = _free_port()
    app = create_app(ServerConfig(db_path=str(tmp_path / "drill.db")),
                     Store(str(tmp_path / "drill.db")))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:  # pragma: no cover - a hung server fails loudly
            raise RuntimeError("hub did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def worker_env(monkeypatch, hub_url):
    """The environment the coordinator hands its workers.

    Set on the parent process rather than passed in, because that is how a
    drill is really run — the hub is configured once and everything under
    it inherits it.
    """
    monkeypatch.setenv("SWITCHBOARD_URL", hub_url)
    monkeypatch.setenv("SWITCHBOARD_WORKSPACE", WS)
    return hub_url


def _run(hub_url, **kwargs):
    config = ClientConfig(url=hub_url, workspace=WS)
    with Client(config, agent_id="drill-coordinator-test") as hub:
        return drill.run_drill(hub, timeout=60, **kwargs)


# --- the shape of a run, without spending a subprocess ----------------------


def test_build_workers_gives_each_worker_its_own_slot_and_id():
    brief = drill.Brief(run_id="r1", task="do a thing")
    workers = drill.build_workers(brief, count=3, kind="builtin")
    assert [w.slot for w in workers] == ["w1", "w2", "w3"]
    assert len({w.agent_id for w in workers}) == 3
    assert all(w.agent_id.startswith("drill-r1-") for w in workers)


def test_claude_workers_carry_the_protocol_in_their_prompt():
    brief = drill.Brief(run_id="r2", task="count the tests")
    argv = drill.build_workers(brief, count=1, kind="claude", model="sonnet")[0].argv
    assert argv[0] == "claude" and argv[1] == "-p"
    prompt = argv[2]
    assert "count the tests" in prompt
    assert drill.brief_key("r2") in prompt        # told to read the brief
    assert drill.result_prefix("r2") in prompt    # told where to report
    assert drill.channel_for("r2") in prompt      # told where to speak
    assert argv[3:] == ["--model", "sonnet"]


def test_custom_workers_require_a_command():
    brief = drill.Brief(run_id="r3", task="t")
    with pytest.raises(ValueError):
        drill.build_workers(brief, count=1, kind="custom")
    worker = drill.build_workers(brief, count=1, kind="custom",
                                 worker_cmd="./agent.sh {slot} {run_id}")[0]
    assert worker.argv == ["./agent.sh", "w1", "r3"]


def test_a_worker_that_never_reported_is_silent_not_failed():
    """The distinction the whole report is built around."""
    silent = drill.Worker(slot="w1", agent_id="a", argv=[])
    failed = drill.Worker(slot="w2", agent_id="b", argv=[], result={"ok": False})
    passed = drill.Worker(slot="w3", agent_id="c", argv=[], result={"ok": True})
    assert silent.verdict() == "silent"
    assert failed.verdict() == "failed"
    assert passed.verdict() == "passed"


# --- end to end -------------------------------------------------------------


def test_drill_runs_workers_and_reports_through_the_hub(worker_env, hub_url):
    report = _run(hub_url, task="protocol check", count=2, kind="builtin")

    assert report["ok"], json.dumps(report, indent=2)
    assert report["counts"] == {"workers": 2, "passed": 2, "failed": 0, "silent": 0}
    assert not report["timed_out"]

    for worker in report["workers"]:
        # Every one of these was learned from the hub, not from the process.
        assert worker["reported"] and worker["ok"]
        assert worker["announced_s"] is not None
        assert worker["result_s"] is not None
        assert worker["messages"] >= 2          # "up" and "done"
        assert worker["leases"]                 # it held its own slot
        names = {c["name"] for c in worker["checks"]}
        assert {"brief-readable", "peers-visible", "own-slot-lease", "shared-lease"} <= names

    telemetry = report["telemetry"]
    assert telemetry["channel_messages"] >= 4
    assert telemetry["announce_latency_s"]["observed"] == 2
    assert telemetry["observer_errors"] == []


def test_workers_read_the_brief_and_contend_for_one_lease(worker_env, hub_url):
    """Two things the drill is actually measuring about the hub: that a
    brief written by one agent is readable by another, and that a key two
    agents reach for is granted to exactly one."""
    report = _run(hub_url, task="contend", count=3, kind="builtin")

    def check(worker, name):
        return next(c for c in worker["checks"] if c["name"] == name)

    assert all(check(w, "brief-readable")["ok"] for w in report["workers"])
    winners = [w for w in report["workers"]
               if "won" in check(w, "shared-lease")["detail"]]
    assert len(winners) == 1, "exactly one worker may hold the contended key"


def test_verify_command_failure_fails_the_drill_without_losing_telemetry(
    worker_env, hub_url
):
    report = _run(hub_url, task="run the checks", count=2, kind="builtin",
                  verify="exit 3")

    assert not report["ok"]
    assert report["counts"]["failed"] == 2
    assert report["counts"]["silent"] == 0      # they failed honestly, and said so
    for worker in report["workers"]:
        verify = next(c for c in worker["checks"] if c["name"] == "verify")
        assert verify["ok"] is False
        assert worker["announced_s"] is not None


def test_verify_command_success_passes(worker_env, hub_url):
    report = _run(hub_url, task="run the checks", count=1, kind="builtin",
                  verify="echo all good")
    assert report["ok"]
    verify = next(c for c in report["workers"][0]["checks"] if c["name"] == "verify")
    assert verify["ok"] and "all good" in verify["detail"]


def test_a_worker_that_says_nothing_is_reported_silent(worker_env, hub_url):
    """The failure mode a drill exists to catch: a launched agent that never
    reaches the hub at all. Its exit status is irrelevant — this one exits
    0 — because a coordinator only ever gets to see the hub."""
    report = _run(hub_url, task="ghost", count=1, kind="custom",
                  worker_cmd="true")

    assert not report["ok"]
    assert report["counts"]["silent"] == 1
    worker = report["workers"][0]
    assert worker["reported"] is False
    assert worker["ok"] is None                 # not False: nothing was claimed
    assert worker["announced_s"] is None


def test_report_is_left_on_the_board(worker_env, hub_url):
    report = _run(hub_url, task="leave a trace", count=1, kind="builtin")
    with Client(ClientConfig(url=hub_url, workspace=WS), agent_id="reader") as hub:
        stored = hub.board_get(drill.report_key(report["run_id"]))
        brief = hub.board_get(drill.brief_key(report["run_id"]))
    assert stored["run_id"] == report["run_id"]
    assert stored["counts"] == report["counts"]
    assert brief["task"] == "leave a trace"


def test_drill_works_on_a_sealed_workspace(worker_env, hub_url, monkeypatch):
    """Encryption is the project's default posture, and board keys are
    blinded before they leave the client — so a result collector written as
    a prefix listing would report every worker silent here while passing
    everywhere else."""
    from switchboard.crypto import generate_key

    key = generate_key()
    monkeypatch.setenv("SWITCHBOARD_KEY", key)
    config = ClientConfig(url=hub_url, workspace=WS, key=key)
    with Client(config, agent_id="drill-coordinator-sealed") as hub:
        report = drill.run_drill(hub, task="sealed", count=2, kind="builtin", timeout=60)
    assert report["ok"], json.dumps(report, indent=2)
    assert report["counts"]["silent"] == 0


# --- the command ------------------------------------------------------------


def test_cli_drill_exit_codes_and_json(worker_env, capsys, tmp_path):
    out = tmp_path / "report.json"
    code = main(["drill", "cli check", "-n", "1", "--worker", "builtin",
                 "--json", "--out", str(out)])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["counts"]["workers"] == 1
    assert json.loads(out.read_text())["run_id"] == report["run_id"]

    code = main(["drill", "cli check", "-n", "1", "--worker", "builtin",
                 "--verify", "exit 1", "--json"])
    assert code == 1


def test_cli_drill_human_output_names_every_worker(worker_env, capsys):
    assert main(["drill", "human check", "-n", "2", "--worker", "builtin"]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "w1" in out and "w2" in out
    assert "brief-readable" in out


def test_cli_refuses_custom_without_a_command(worker_env, capsys):
    assert main(["drill", "t", "--worker", "custom"]) == 1
    assert "--worker-cmd" in capsys.readouterr().err
