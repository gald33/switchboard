# wake-on-message — park on the hub until something arrives, then exit so the
# agent runner wakes the session.
#
# A shim. The listener is `switchboard listen`, and this file exists so that a
# repo `init` has set up can arm one with a path rather than having to know the
# hub and workspace: the lines above define `sb` and export both, exactly as
# they do for the lifecycle hooks.
#
# Prefer the command where you can, because it takes the flags every other
# command takes — `-w` for another room, `--invite` to be handed one along with
# its key. This wrapper bakes in one room and cannot leave it, which is right
# for a repo's own agents and wrong for anything crossing repos:
#
#   switchboard listen --until forecast:p50
#   switchboard -w task/migrate-auth listen --until +900
#   switchboard --invite swb1_… listen
#
# Run it as a background process. The exit *is* the wake, so it is one wake and
# not a subscription: a session still waiting has to arm it again. It peeks
# rather than drains — it shares a read cursor with the session it serves — so
# the woken session calls `inbox` itself to take delivery.
#
# Exit codes: 0 a message arrived (on stdout), 2 the deadline passed with
# nothing to report, 1 it never watched anything.
#
# Usage:
#   sh .switchboard/wake-on-message.sh --until forecast:p50
#   sh .switchboard/wake-on-message.sh --until +900 -c deploys

# Not `exec`: `sb` is a shell function defined above, and exec needs a real
# command. Caught by the test that runs this file rather than the command.
sb listen "$@"
