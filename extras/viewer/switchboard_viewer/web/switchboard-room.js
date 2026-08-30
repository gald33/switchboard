// Reading a room from a browser: the same view `switchboard_viewer/viewer.py` builds,
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

// How many pages the conversation read walks looking for the tail before it
// gives up. A runaway guard rather than a policy — messages expire, so a live
// room does not grow without bound — and the mirror of `_MAX_TAIL_PAGES` in
// the client.
const MAX_TAIL_PAGES = 100;

// --- invites ----------------------------------------------------------------
//
// The read half of `invite.py`, for the same reason the cipher has one: this
// page is where a person who was handed a room actually arrives, and asking
// them to split one string into four fields by hand reintroduces exactly the
// mistake an invite exists to remove. Four chances to differ become one, and a
// wrong one fails at the parse instead of an hour later in an empty room.
//
// Kept byte-compatible with the Python encoder — same prefix, same version,
// same single-letter keys, same unpadded base64url — and held there by
// `tests/test_web_reader.py`, which encodes with `Invite` and decodes here.

export const INVITE_PREFIX = "swb1_";
export const INVITE_VERSION = 1;
export const PROBE_SENTINEL = "switchboard-room-proof";

export class InviteError extends Error {}

/** Write one, which is the half this page did not have.
 *
 *  Only ever for a room this browser is already holding: the page mints
 *  nothing, and an invite it produces grants exactly what its reader already
 *  had, no more. That is also why it stays a client rather than becoming a
 *  second way to make rooms.
 *
 *  Byte-compatible with `Invite.encode`, and held there by
 *  `tests/test_web_invite.py`, which encodes here and decodes with the Python
 *  `Invite` — the same parity the reader half is held to, in the other
 *  direction. Field order matters for nothing but reading the two side by
 *  side, so it is the Python one.
 */
export function encodeInvite(room) {
  const payload = {
    v: INVITE_VERSION,
    u: room.url || "",
    w: room.workspace || "",
    t: room.token || "",
    k: room.key || "",
    n: room.note || "",
    p: room.probe || "",
    ki: room.keyId || "",
    wt: room.workspaceToken || "",
  };
  if (!payload.u) throw new InviteError("an invite needs a hub URL");
  if (!payload.w) throw new InviteError("an invite needs a workspace");
  const raw = new TextEncoder().encode(JSON.stringify(payload));
  return INVITE_PREFIX + btoa(String.fromCharCode(...raw))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Read an invite, or throw. Never a partial join: a string this cannot fully
 *  understand is refused rather than half-applied. */
export function decodeInvite(text) {
  const blob = String(text ?? "").trim();
  if (!blob.startsWith(INVITE_PREFIX)) {
    throw new InviteError(
      `not a switchboard invite (expected it to start with '${INVITE_PREFIX}'). ` +
      "Invites are produced by `switchboard invite` and pasted whole.");
  }
  let payload;
  try {
    const body = blob.slice(INVITE_PREFIX.length)
      .replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(body + "=".repeat((4 - (body.length % 4)) % 4));
    payload = JSON.parse(new TextDecoder().decode(
      Uint8Array.from(raw, (c) => c.charCodeAt(0))));
  } catch (e) {
    throw new InviteError(`invite is corrupt or truncated (${e.message})`);
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new InviteError("invite did not contain an object");
  }
  if (payload.v !== INVITE_VERSION) {
    throw new InviteError(
      `invite is version ${JSON.stringify(payload.v)}, this page understands ` +
      `${INVITE_VERSION}. Get a newer page rather than editing the string.`);
  }
  if (typeof payload.u !== "string" || !payload.u) {
    throw new InviteError("invite has no hub URL");
  }
  if (typeof payload.w !== "string" || !payload.w) {
    throw new InviteError("invite has no workspace");
  }
  return {
    url: payload.u.replace(/\/$/, ""),
    workspace: payload.w,
    token: String(payload.t ?? "") || null,
    key: String(payload.k ?? "") || null,
    note: String(payload.n ?? ""),
    probe: String(payload.p ?? ""),
    // Which key opens this room, when the invite names one it did not carry.
    // A browser has no `SWITCHBOARD_KEY_<ID>` to look it up in, so the id is
    // not a lookup here — it is what lets the page say *which* key to paste
    // instead of "no key", which is the difference between a question a
    // reader can answer and one they cannot.
    keyId: String(payload.ki ?? ""),
    // The room's token, of which `workspace` is the hash. A browser has no
    // rooms file to write it into, so nothing here uses it — it is surfaced so
    // that a reader who *does* have a checkout can copy it out rather than
    // being handed an identifier that cannot be turned back into a record.
    workspaceToken: String(payload.wt ?? ""),
  };
}

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
        } else if (context === "board.value" && isBoardLabelled(opened)) {
          // Same move, same reason: a board key is stored blinded, so the
          // readable one travels sealed beside the value.
          item.hub_key = item.key;
          item.key = opened.k;
          item[field] = opened.v;
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
    if (item.key !== undefined && item.hub_key === undefined) {
      item.hub_key = item.key;
    }
  }
  return items;
}

function isLabelled(opened) {
  if (!opened || typeof opened !== "object") return false;
  const keys = Object.keys(opened).sort().join(",");
  return (keys === "b,ch" || keys === "b,ch,s") && typeof opened.ch === "string";
}

function isBoardLabelled(opened) {
  if (!opened || typeof opened !== "object") return false;
  return Object.keys(opened).sort().join(",") === "k,t,v"
    && opened.t === "sbk1" && typeof opened.k === "string";
}

const WRONG_ROOM =
  "WRONG ROOM: reached this hub and workspace, but could not read the proof " +
  "the inviter left. Your key does not match theirs — you would appear on " +
  "each other's roster and be unable to exchange anything. Ask for a fresh " +
  "invite rather than editing settings";

/** What an invite's proof-of-room says, or null when there is nothing to say.
 *
 *  Only failures. Opening a value the inviter sealed proves the hub, the
 *  workspace *and* the key all match — but a viewer that announced every
 *  success would be shouting the normal case, and these notes are drawn as
 *  warnings. Silence is the good outcome.
 *
 *  Free, because the board has already been read: the probe is an ordinary
 *  entry, so no extra request is made to check it.
 */
function probeVerdict(probe, board) {
  const entry = board.find((e) => e.key === probe);
  if (entry) return entry.value === PROBE_SENTINEL ? null : WRONG_ROOM;
  // Not found is not the same as absent. A board key travels sealed beside
  // its value, so a key this room cannot open never comes back as a name —
  // the inviter's probe is sitting right there under a token neither side can
  // match. So an unopened entry is the answer, not a missing one.
  if (board.some((e) => e.sealed)) return WRONG_ROOM;
  return "this invite carries a proof-of-room and it is not on the " +
         "blackboard — it may have expired, or this may not be the room the " +
         "invite described";
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

/** The newest `limit` messages per channel, paged out of a forward-only hub.
 *
 *  Mirrors `_Tail` in `switchboard/client.py`, down to how far the cursor is
 *  allowed to move: one `since` covers every channel in the request, so it
 *  advances only to the lowest of the pages that came back full. Anything
 *  further steps over the next message in a quieter channel — a message
 *  silently lost rather than a page saved. The overlap that follows is why
 *  messages are kept by sequence instead of appended.
 */
async function readTail(config, tokens, limit) {
  const width = Math.max(1, limit);
  const kept = new Map();  // hub channel -> Map(seq -> message)
  let since = 0;
  for (let page = 0; page < MAX_TAIL_PAGES; page++) {
    const messages = (await get(config, "/inbox", {
      agent_id: "viewer", channel: tokens, since, peek: true,
      include_own: true, limit,
    })).messages;
    const seen = new Map();
    for (const m of messages) {
      const channel = m.hub_channel ?? m.channel ?? "";
      const seq = Number(m.seq ?? 0);
      if (!kept.has(channel)) kept.set(channel, new Map());
      const window = kept.get(channel);
      window.set(seq, m);
      for (const old of [...window.keys()].sort((a, b) => a - b).slice(0, -width)) {
        window.delete(old);
      }
      seen.set(channel, (seen.get(channel) ?? []).concat(seq));
    }
    const full = [...seen.values()]
      .filter((seqs) => seqs.length >= width)
      .map((seqs) => Math.max(...seqs));
    if (!full.length) break;
    const next = Math.min(...full);
    if (next <= since) break;
    since = next;
  }
  const out = [];
  for (const window of kept.values()) out.push(...window.values());
  out.sort((a, b) => Number(a.seq ?? 0) - Number(b.seq ?? 0));
  return out;
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
      // null until an invite gives us something to check. See below.
      verified: null,
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
    notes.push(`${mismatched} agent(s) here hold a different key — ` +
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
    // Readable now unless it stayed a token — see the Python builder.
    key: e.key, sealed: Boolean(room) && e.key === e.hub_key,
    value: e.value, revision: e.revision,
    updated_by: who(e.updated_by), updated_at: e.updated_at,
    expires_in: e.expires_in,
  }));

  if (config.probe) {
    const verdict = probeVerdict(config.probe, view.board);
    if (verdict) notes.push(verdict);
    // A failure is a warning and a success is a fact, and the page has a place
    // for each: the notes strip shouts WRONG ROOM, and a quiet word beside the
    // room name says the proof opened. Silence was right when there was
    // nowhere to put the good outcome.
    view.hub.verified = verdict === null;
    // And the probe itself comes off the board — this viewer's machinery, not
    // state an agent published, carrying a sentinel that means nothing to a
    // reader. The Python builder drops it in the same place.
    view.board = view.board.filter((e) => e.key !== config.probe);
  }

  let channels = (await section("the channel list",
    async () => (await get(config, "/channels")).channels)) ?? [];
  if (channels.length > MAX_CHANNELS) {
    // Volume alone drops the channel that was created this morning in favour
    // of one that was busy last week and is finished — which is exactly
    // backwards for a page whose job is to say where the work is now. Recency
    // leads, and volume breaks its ties.
    notes.push(`showing ${MAX_CHANNELS} of ${channels.length} channels — ` +
               "the ones that moved most recently");
    channels = [...channels].sort((a, b) =>
      (b.latest_at ?? 0) - (a.latest_at ?? 0) || b.messages - a.messages
    ).slice(0, MAX_CHANNELS);
  }

  const tokens = channels.map((c) => c.channel);
  // The tail of the room, not its opening. `peek` leaves every cursor where
  // it was, but the hub only reads *forward* — `since=N` answers with the
  // `limit` messages after N — so a single `since=0` read is the first fifty
  // messages of the room forever, however long the room has been running.
  // Getting to the newest ones means paging there; `readTail` does the walk.
  const opened = tokens.length ? ((await section("the conversation", async () =>
    openAll(room, await readTail(config, tokens, limit), SEALED.messages))) ?? []) : [];

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
               "key in settings to read it");
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
