// Reading a room from a browser: the same view `examples/viewer.py` builds,
// assembled on this side of the wire instead.
//
// This is deliberately a mirror of `snapshot()` in the Python viewer, down to
// the field names, because one page renders both. The two are kept honest by
// `tests/test_web_page.py`, which drives a real browser against a real hub and
// compares what this produces with what the Python builds for the same room.
//
// What it will not do is what the Python one will not do: no posting, no
// registering, and every read leaves cursors where it found them.

import { RoomKey, looksSealed } from "./switchboard-open.js";

/** Fields that arrive sealed, and the context each was sealed under. Getting
 *  a context wrong does not fail loudly — it fails as one blank column — so
 *  this table is copied from `_OPEN_RESPONSE` rather than retyped. */
const SEALED = {
  agents: { name: "agent.name", branch: "agent.branch", task: "agent.task" },
  leases: { note: "lease.note" },
  entries: { value: "board.value" },
  messages: { body: "message.body" },
};

const MAX_CHANNELS = 60;

class HubError extends Error {}

async function get(config, path, params = {}) {
  const url = new URL(config.url.replace(/\/$/, "") + path);
  url.searchParams.set("workspace", config.workspace);
  for (const [name, value] of Object.entries(params)) {
    for (const one of Array.isArray(value) ? value : [value]) {
      url.searchParams.append(name, one);
    }
  }
  const headers = config.token ? { Authorization: `Bearer ${config.token}` } : {};
  let response;
  try {
    response = await fetch(url, { headers, cache: "no-store" });
  } catch (e) {
    // A browser will not tell a page why a cross-origin request failed, so
    // guessing precisely here would be dishonest. Name the two causes.
    throw new HubError(
      "cannot reach the hub — it may be down, or it may not allow this page's " +
      "origin (the hub needs SWITCHBOARD_CORS_ORIGINS to include " +
      `${location.origin})`);
  }
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = await response.json();
      detail = body.detail || body.error || detail;
    } catch { /* a body that is not JSON tells us nothing more */ }
    throw new HubError(String(detail));
  }
  return response.json();
}

/** Open every sealed field in one response, marking what this key cannot.
 *
 *  Marking rather than throwing, for messages as well as the roster: a reader
 *  meets rooms it was never subscribed to, so a foreign key is expected here
 *  in a way it never is in an agent's inbox. The Python client draws the same
 *  line for the same reason.
 */
async function openAll(room, items, fields) {
  for (const item of items) {
    for (const [field, context] of Object.entries(fields)) {
      const value = item[field];
      if (value === null || value === undefined) continue;
      if (!room) {
        // No key. Nothing was attempted, so an envelope arrives intact — and
        // "empty" must not render the same as "sealed and unopenable".
        if (looksSealed(value)) {
          item[field] = null;
          item.unreadable = true;
        }
        continue;
      }
      try {
        const opened = await room.open(value, context);
        if (context === "message.body" && isLabelled(opened)) {
          // The plaintext channel name travels inside the ciphertext, which
          // is the only way a reader recovers a name from a blinded token.
          item.hub_channel = item.channel;
          item.channel = opened.ch;
          item[field] = opened.b;
        } else {
          item[field] = opened;
        }
      } catch {
        item[field] = null;
        item.unreadable = true;
      }
    }
    if (item.channel !== undefined && item.hub_channel === undefined) {
      item.hub_channel = item.channel;
    }
  }
  return items;
}

function isLabelled(opened) {
  if (!opened || typeof opened !== "object") return false;
  const keys = Object.keys(opened).sort().join(",");
  return (keys === "b,ch" || keys === "b,ch,s") && typeof opened.ch === "string";
}

/** The timing forecast a sender folded into a body, split back out. */
function unwrapForecast(body) {
  if (body && typeof body === "object" && !Array.isArray(body)
      && "timing_forecast" in body
      && Object.keys(body).every((k) => k === "text" || k === "timing_forecast")) {
    return [body.text ?? null, body.timing_forecast];
  }
  return [body, null];
}

/** One complete view of a room, in the shape the page renders. */
export async function snapshot(config, { limit = 50, refresh = 3 } = {}) {
  const room = config.key ? RoomKey.from(config.key, config.workspace) : null;
  const notes = [];
  const view = {
    generated_at: new Date().toISOString(),
    version: "browser",
    refresh_ms: Math.max(500, refresh * 1000),
    hub: {
      url: config.url, workspace: config.workspace,
      encrypted: Boolean(room), reachable: true,
    },
    agents: [], leases: [], board: [], channels: [], messages: [], notes,
  };

  const section = async (what, read) => {
    try {
      return await read();
    } catch (e) {
      notes.push(e instanceof HubError && /cannot reach/.test(e.message)
        ? e.message : `the hub refused ${what}: ${e.message}`);
      if (e instanceof HubError && /cannot reach/.test(e.message)) {
        view.hub.reachable = false;
      }
      return null;
    }
  };

  const roster = await section("the roster",
    async () => openAll(room, (await get(config, "/agents")).agents, SEALED.agents));
  const names = {};
  for (const agent of roster ?? []) {
    if (agent.name) names[agent.agent_id] = agent.name;
  }
  const who = (id) => ({ id: id || "", name: names[id] ?? null });
  view.agents = (roster ?? []).map((a) => ({
    id: a.agent_id, name: a.name, kind: a.kind, branch: a.branch, task: a.task,
    channels: a.channels || [], last_seen_at: a.last_seen_at,
    expires_in: a.expires_in, stale: Boolean(a.stale),
    unreadable: Boolean(a.unreadable),
  }));
  const mismatched = view.agents.filter((a) => a.unreadable).length;
  if (mismatched) {
    notes.push(`${mismatched} agent(s) here hold a different workspace key — ` +
               "you cannot read their messages and they cannot read yours");
  }

  const leases = await section("leases",
    async () => openAll(room, (await get(config, "/leases")).leases, SEALED.leases));
  view.leases = (leases ?? []).map((l) => ({
    resource: l.resource, sealed: Boolean(room), holder: who(l.holder),
    note: l.note, expires_in: l.expires_in, acquired_at: l.acquired_at,
  }));

  const board = await section("the blackboard",
    async () => openAll(room, (await get(config, "/board")).entries, SEALED.entries));
  view.board = (board ?? []).map((e) => ({
    key: e.key, sealed: Boolean(room), value: e.value, revision: e.revision,
    updated_by: who(e.updated_by), updated_at: e.updated_at,
    expires_in: e.expires_in,
  }));

  let channels = (await section("the channel list",
    async () => (await get(config, "/channels")).channels)) ?? [];
  if (channels.length > MAX_CHANNELS) {
    notes.push(`showing ${MAX_CHANNELS} of ${channels.length} channels — ` +
               "the busiest by message count");
    channels = [...channels].sort((a, b) => b.messages - a.messages).slice(0, MAX_CHANNELS);
  }

  const tokens = channels.map((c) => c.channel);
  // One request for the whole room: `since=0` with `peek` reads from the
  // beginning and leaves every cursor where it was.
  const opened = tokens.length ? ((await section("the conversation", async () =>
    openAll(room, (await get(config, "/inbox", {
      agent_id: "viewer", channel: tokens, since: 0, peek: true,
      include_own: true, limit,
    })).messages, SEALED.messages))) ?? []) : [];

  const byToken = new Map(tokens.map((t) => [t, []]));
  for (const m of opened) {
    if (!byToken.has(m.hub_channel)) byToken.set(m.hub_channel, []);
    byToken.get(m.hub_channel).push(m);
  }

  let keyless = false;
  for (const entry of channels) {
    const here = byToken.get(entry.channel) ?? [];
    const named = here.find((m) => m.channel && m.channel !== entry.channel);
    const label = named ? named.channel : entry.channel;
    const unreadable = here.length > 0 && here.every((m) => m.unreadable);
    view.channels.push({
      token: entry.channel, name: label, named: label !== entry.channel,
      dm: label.startsWith("@"), count: entry.messages,
      latest_at: entry.latest_at ? new Date(entry.latest_at * 1000).toISOString() : null,
      unreadable,
    });
    for (const m of here) {
      let [body, forecast] = unwrapForecast(m.body);
      if (m.unreadable) {
        keyless = keyless || !room;
        body = null;
        forecast = null;
      }
      view.messages.push({
        seq: m.seq, channel: label, token: entry.channel,
        dm: label.startsWith("@"), from: who(m.from),
        type: m.type || "note", body, sealed_body: Boolean(m.unreadable),
        forecast, thread: m.thread, created_at: m.created_at,
      });
    }
  }

  const named = new Map(view.channels.filter((c) => c.named).map((c) => [c.token, c.name]));
  for (const agent of view.agents) {
    agent.channels = agent.channels.map((c) => named.get(c) ?? c);
  }

  if (keyless || (!room && view.agents.some((a) => a.unreadable))) {
    notes.push("this room is encrypted and no key is set here: add the " +
               "workspace key in settings to read it");
  }
  if (view.channels.some((c) => c.unreadable)) {
    notes.push("some channels are sealed under a different key and are listed " +
               "without their messages");
  }

  view.messages.sort((a, b) => a.seq - b.seq);
  view.notes = [...new Set(notes)];
  return view;
}

/** The cheap per-room line for the switcher: is anyone in there. */
export async function summarise(config) {
  const out = {
    id: `${config.url}/${config.workspace}`, label: config.label || config.workspace,
    source: config.source || "settings", hub: config.url, workspace: config.workspace,
    encrypted: Boolean(config.key), awake: null, error: null,
  };
  try {
    const { agents } = await get(config, "/agents");
    out.awake = agents.filter((a) => !a.stale).length;
  } catch (e) {
    out.error = e.message;
  }
  return out;
}
