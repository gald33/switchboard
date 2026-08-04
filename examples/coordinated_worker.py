#!/usr/bin/env python3
"""A worker that coordinates with other agents through a Switchboard hub.

Run several copies at once, against the same hub and workspace, and watch them
divide the work between them without any of them being told about the others:

    export SWITCHBOARD_URL=http://127.0.0.1:8787
    export SWITCHBOARD_TOKEN=dev-token
    export SWITCHBOARD_WORKSPACE=demo

    python examples/coordinated_worker.py alice &
    python examples/coordinated_worker.py bob &
    python examples/coordinated_worker.py carol &

Two mechanisms are doing two different jobs here, and the distinction is the
whole lesson of this example:

*A lease says "I am working on this right now."* It provides mutual exclusion
and it expires, so a worker that dies never blocks the task forever.

*A blackboard entry says "this has been finished."* It outlives the lease on
purpose. Without it a worker arriving later finds the lease released, assumes
the task is free, and does it a second time — mutual exclusion at any instant
is not the same as doing something exactly once.

So each task is checked against the blackboard, claimed, checked again (in case
somebody finished it between the check and the claim), worked, recorded, and
only then released.

Kill a worker mid-task with `kill -9` to see the other half: it never releases
its lease, because nothing runs on the way out — and another worker picks the
task up anyway once the lease expires.
"""

from __future__ import annotations

import random
import sys
import time

from switchboard import Client, LeaseHeld, SwitchboardError, detect_identity

# The shared work list. In a real system this is your ticket queue, a list of
# files to migrate, a set of packages to upgrade — anything partitionable.
TASKS = [
    "migrate/orders",
    "migrate/customers",
    "migrate/invoices",
    "migrate/payments",
    "migrate/shipping",
    "migrate/inventory",
]

CHANNEL = "migration"
LEASE_TTL = 60  # short, so a killed worker's task frees up quickly
DONE_TTL = 3600  # completion records outlive the leases that produced them


def done_key(task: str) -> str:
    return f"done/{task}"


def work_on(task: str) -> str:
    """Stand-in for the real thing."""
    duration = random.uniform(1.5, 4.0)
    time.sleep(duration)
    return f"took {duration:.1f}s"


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else None
    identity = detect_identity(agent_id=name)

    with Client(agent_id=identity.agent_id) as hub:
        try:
            hub.register(
                name=name or identity.name,
                kind=identity.kind,
                branch=identity.branch,
                channels=[CHANNEL],
            )
        except (SwitchboardError, OSError) as exc:
            print(f"cannot reach hub at {hub.config.url}: {exc}", file=sys.stderr)
            return 1

        me = identity.agent_id
        print(f"[{me}] joined; {len(TASKS)} tasks on the list")

        done: list[str] = []
        skipped = 0
        for task in TASKS:
            # 1. Already finished by someone? Cheapest check first.
            if hub.board_get(done_key(task)) is not None:
                skipped += 1
                continue

            # 2. Claim it. If another worker holds it, move on rather than
            #    waiting — that is what makes the workers parallel instead of
            #    merely concurrent.
            try:
                hub.acquire(task, note=f"working {task}", ttl=LEASE_TTL)
            except LeaseHeld as exc:
                print(f"[{me}] {task} -> in progress by {exc.holder}, skipping")
                continue

            try:
                # 3. Check again now that we hold the lease. Someone may have
                #    finished and released between our check and our claim.
                record = hub.board_get(done_key(task))
                if record is not None:
                    print(f"[{me}] {task} -> finished by {record['by']} just now, skipping")
                    skipped += 1
                    continue

                print(f"[{me}] {task} -> claimed, working")
                detail = work_on(task)

                # Heartbeat on anything long: renews every lease this agent
                # holds and collects what other agents have said meanwhile.
                state = hub.heartbeat(task=f"finishing {task}")
                for message in hub.inbox():
                    print(f"[{me}]   <- {message['from']}: {message['body']}")

                # 4. Record completion BEFORE releasing, so there is no window
                #    in which the task is both unclaimed and unrecorded.
                hub.board_set(
                    done_key(task),
                    {"by": me, "detail": detail, "at": time.time()},
                    ttl=DONE_TTL,
                )
                hub.post(CHANNEL, {"task": task, "by": me, "detail": detail}, type="completed")
                done.append(task)
                print(f"[{me}] {task} -> done ({detail}), "
                      f"holding {len(state['leases'])} lease(s)")
            finally:
                # 5. Release in a finally so a crash frees the task immediately
                #    rather than making everyone wait out the TTL.
                hub.release(task)

        hub.post(CHANNEL, f"{me} finished: {len(done)} task(s)", type="summary")
        print(f"[{me}] finished {len(done)}, skipped {skipped} already-done: "
              f"{', '.join(done) or 'nothing'}")

        # Leave cleanly so the roster is accurate immediately rather than
        # after the presence TTL lapses.
        hub.deregister()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted — any held leases will expire on their own")
        raise SystemExit(130) from None
