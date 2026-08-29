// Painting a room, wherever the reading was done.
//
// One renderer, two callers: `switchboard_viewer/viewer.py` builds the state in Python
// and serves it over `api/state`; `switchboard_viewer/web/index.html` builds the same
// state in the browser with `switchboard-room.js`. Sharing this file is what
// keeps them one product rather than two that drift — the state shape is the
// contract, and `tests/test_web_page.py` holds the two builders to it.

// --- what you are reading ----------------------------------------------------
//
// A channel was the only way to narrow the conversation, which leaves the two
// questions a reader most often arrives with unanswerable: what did this agent
// say, and where was that message about the lexer. All three narrow the same
// list and all three are said in the same heading, so no combination of them
// can leave you looking at less than you think.

export let filter = null;   // channel name, or null for everything
export let query = "";      // free text over message bodies
export let sender = null;   // agent id, or null

let onScope = null;         // told when any of the three change

export function setFilter(value) { filter = value; toBottom = true; }

/** The whole scope, for a caller that keeps it somewhere — the URL, say. */
export function scope() { return { c: filter, q: query, from: sender }; }

export function setScope({ c = null, q = "", from = null } = {}) {
  filter = c || null;
  query = q || "";
  sender = from || null;
  toBottom = true;
}

/** Told after every change a reader makes, never for one restored from a URL —
 *  otherwise restoring a scope would immediately rewrite the thing it came
 *  from. */
export function onScopeChange(fn) { onScope = fn; }

function scopeChanged() {
  toBottom = true;   // a narrower list starts at its newest, not mid-way
  if (onScope) onScope(scope());
  if (last) render(last);
}

// --- following the conversation ---------------------------------------------
//
// The pane is repainted every few seconds, which by itself sends the reader
// back to the oldest message on every refresh. So the scroll position is
// carried across the repaint, and the rule is the one every chat window uses:
// stay pinned to the newest while the reader is at the bottom, and stop the
// moment they scroll up to read something — a view that yanks itself away
// mid-sentence is worse than one that never moved.
//
// Being scrolled up then has to be survivable, which is what the pill is for:
// it says traffic arrived without moving anything, and clicking it re-pins.

const NEAR_BOTTOM = 40;   // px of slack, so a half-pixel row height still counts

let toBottom = true;      // next paint should land at the newest
let following = null;     // room these scroll numbers belong to
let seenSeq = 0;          // newest seq the reader has actually been shown
let newestSeq = 0;
let wired = false;
let painted = false;      // a room has been drawn, so arrivals are arrivals
let lastScope = null;     // the scope those rows were drawn under

// The last state and handlers, so a click on a chip can repaint without
// waiting for the next poll and without the caller re-plumbing them.
let last = null;
let handlers = {};

function atBottom(pane) {
  return pane.scrollHeight - pane.scrollTop - pane.clientHeight < NEAR_BOTTOM;
}

function stickToBottom(pane) {
  pane.scrollTop = pane.scrollHeight;
  seenSeq = newestSeq;
}

function drawJump() {
  const jump = $("jump");
  if (!jump) return;   // a page that renders one static state has no use for it
  const behind = newestSeq - seenSeq;
  jump.hidden = behind <= 0;
  jump.textContent = behind === 1 ? "1 new message ↓" : `${behind} new messages ↓`;
}

const $ = (id) => document.getElementById(id);

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// --- keeping the page still --------------------------------------------------
//
// Every panel here was previously rebuilt with `innerHTML` on each poll, three
// seconds apart. That is a lot of damage for a page whose content barely
// changes: it cancels the selection you were dragging, drops focus if you had
// tabbed to a chip, resets every hover, and flashes the lot on a slow machine.
// Worse, it makes anything the reader adjusts — an expanded value, a scroll
// position inside a row — impossible to keep, because the node holding it is
// gone.
//
// So rows are keyed and reconciled instead. A row whose markup is unchanged is
// left strictly alone, which is the common case: a poll usually adds one
// message and moves one timestamp. `sync` returns the nodes it created, which
// is also how the conversation knows which messages just arrived.

function sync(container, rows) {
  const have = new Map();
  for (const node of container.children) {
    if (node.dataset.key !== undefined) have.set(node.dataset.key, node);
  }
  const made = [];
  let cursor = null;   // the node the next row must follow
  for (const row of rows) {
    let node = have.get(row.key);
    if (node) {
      have.delete(row.key);
      // Only touch the DOM when the markup actually differs. Comparing the
      // string we were about to write is cheaper than writing it, and it is
      // what keeps a selection alive across a poll that changed nothing.
      if (node.dataset.html !== row.html) {
        node.innerHTML = row.html;
        node.dataset.html = row.html;
      }
      if (node.className !== row.cls) node.className = row.cls;
    } else {
      node = document.createElement("div");
      node.dataset.key = row.key;
      node.dataset.html = row.html;
      node.className = row.cls;
      node.innerHTML = row.html;
      made.push(node);
    }
    // Order is the state's order. Moving a node that is already in place is a
    // no-op in every engine, so this costs nothing on a quiet poll.
    const next = cursor ? cursor.nextSibling : container.firstChild;
    if (next !== node) container.insertBefore(node, next);
    cursor = node;
  }
  for (const stale of have.values()) stale.remove();
  // Whatever came before the keyed rows — an "empty" placeholder — is not ours
  // to keep once there is content.
  for (const node of [...container.children]) {
    if (node.dataset.key === undefined) node.remove();
  }
  return made;
}

/** An empty panel, said in the panel's own words. */
function emptyPane(container, text) {
  if (container.children.length === 1 &&
      container.firstChild.dataset?.empty === text) return;
  container.innerHTML = "";
  const node = document.createElement("div");
  node.className = "empty";
  node.dataset.empty = text;
  node.textContent = text;
  container.append(node);
}

// --- clocks ------------------------------------------------------------------
//
// Ages and expiries used to be rendered as text at paint time, so "5s ago" sat
// frozen for three seconds and then jumped to "8s ago" — a room that reads as
// intermittent rather than live, which is the opposite of what a presence
// display is for.
//
// They are markup with an absolute instant in it now, filled in by a ticker
// once a second. Absolute on purpose: the markup for an unchanged row then
// stays byte-identical across polls, which is what lets `sync` leave it alone.

function stamp(iso) {
  return iso ? `<time data-at="${esc(iso)}"></time>` : "";
}

/** A countdown, from a deadline rather than a duration.
 *
 *  `expires_in` is seconds measured when the snapshot was taken; a lease with
 *  four seconds left would otherwise read "4s" for a whole poll and then be
 *  gone. Anchoring it to `generated_at` turns it into a real deadline that
 *  ticks down, and — this is the point — one the reader can see coming.
 */
function countdown(secs, generatedAt) {
  if (secs == null) return "—";
  const at = new Date(Date.parse(generatedAt) + secs * 1000).toISOString();
  return `<time data-until="${esc(at)}"></time>`;
}

const URGENT = 60;    // seconds; below this a claim is about to lapse

let allChips = false;

/** How many channels a glance can use, which is not a constant: twelve chips
 *  are one row on a wide window and four rows on a phone, where they push the
 *  conversation they are meant to organise off the screen. */
function chipCap() {
  return matchMedia("(max-width: 900px)").matches ? 6 : 12;
}

function tick() {
  const now = Date.now();
  for (const el of document.querySelectorAll("time[data-at]")) {
    el.textContent = dur((now - Date.parse(el.dataset.at)) / 1000) + " ago";
  }
  for (const el of document.querySelectorAll("time[data-until]")) {
    const left = (Date.parse(el.dataset.until) - now) / 1000;
    el.textContent = left <= 0 ? "expired" : dur(left);
    el.classList.toggle("urgent", left <= URGENT);
  }
}

function dur(secs) {
  if (secs == null) return "—";
  secs = Math.max(0, Math.round(secs));
  if (secs < 60) return secs + "s";
  if (secs < 3600) return Math.floor(secs / 60) + "m" + String(secs % 60).padStart(2, "0") + "s";
  return Math.floor(secs / 3600) + "h" + String(Math.floor((secs % 3600) / 60)).padStart(2, "0") + "m";
}
function who(w) {
  if (!w || !w.id) return "—";
  return esc(w.name || w.id);
}
function short(token) {
  return token.length > 12 ? token.slice(0, 10) + "…" : token;
}
function ident(text, sealed) {
  // A blinded identifier is not a name and must not be dressed up as one.
  return sealed
    ? `<span class="sealed mono" title="sealed: the hub only ever saw this token">🔒 ${esc(short(text))}</span>`
    : `<span class="mono">${esc(text)}</span>`;
}

/** A body, with a ceiling on it.
 *
 *  One large blackboard value or one pasted payload used to push everything
 *  below it off the page, which on the right-hand column means the panel you
 *  were reading is simply gone. Long bodies are clamped and open on a click —
 *  and stay open across polls, because the node holding that state now
 *  survives them.
 */
const CLAMP_LINES = 15;

/** The text a search is searching. A sealed body has none, and saying "no
 *  match" for a message nobody can read would be a lie of the same shape as
 *  rendering it empty. */
function bodyText(m) {
  if (m.sealed_body) return "";
  if (typeof m.body === "string") return m.body;
  return m.body == null ? "" : JSON.stringify(m.body);
}

function matches(m) {
  if (filter !== null && m.channel !== filter) return false;
  if (sender !== null && !(m.from && m.from.id === sender)) return false;
  if (query && !bodyText(m).toLowerCase().includes(query.toLowerCase())) return false;
  return true;
}

function bodyHtml(body) {
  if (body === null || body === undefined) return `<div class="body sub">(empty)</div>`;
  const text = typeof body === "string" ? body : JSON.stringify(body, null, 2);
  const inner = typeof body === "string"
    ? `<div class="body">${esc(text)}</div>`
    : `<pre>${esc(text)}</pre>`;
  if (text.split("\n").length <= CLAMP_LINES && text.length < 1200) return inner;
  return `<div class="clamp">${inner}<button class="more" type="button">show more</button></div>`;
}

/** The blackboard, as the tree its keys already are.
 *
 *  Hub order is no order at all: the entry that changed thirty seconds ago sits
 *  wherever it happened to land, so the panel has to be re-read from the top
 *  every time. And a blackboard key is a path — `handoff/lexer/state`, not a
 *  name that happens to contain a slash — so a flat list throws away structure
 *  the room itself put there.
 *
 *  So: a tree, with recency running through it. Every branch is placed by the
 *  newest entry beneath it, and so is every leaf, which keeps "what just
 *  changed is near the top" true at each level rather than only at the top. A
 *  branch folds, and carries how much is behind it and when that last moved, so
 *  folding one never hides that something inside it moved.
 *
 *  A branch that leads to exactly one thing is not a branch, it is a longer
 *  name: those compress into a single row, which is also why a lone key with a
 *  prefix never grows a heading of its own.
 *
 *  Blinded keys are not paths — the hub only ever saw a token — so they cannot
 *  join the tree. They are one branch at the end, where the label is the
 *  explanation rather than a gap.
 */
function boardRows(s) {
  const when = (e) => Date.parse(e.updated_at || 0) || 0;

  // --- grow it ---
  const root = { kids: new Map(), entry: null, at: 0, n: 0 };
  for (const e of s.board) {
    if (e.sealed) continue;
    let node = root;
    for (const segment of e.key.split("/").filter(Boolean)) {
      if (!node.kids.has(segment)) {
        node.kids.set(segment, { kids: new Map(), entry: null, at: 0, n: 0 });
      }
      node = node.kids.get(segment);
    }
    node.entry = e;
  }

  // --- and let recency and weight settle back down it ---
  const settle = (node) => {
    node.at = node.entry ? when(node.entry) : 0;
    node.n = node.entry ? 1 : 0;
    for (const kid of node.kids.values()) {
      settle(kid);
      node.at = Math.max(node.at, kid.at);
      node.n += kid.n;
    }
  };
  settle(root);

  const rows = [];
  const leaf = (e, name) => ({
    key: e.key,
    cls: "",
    html: `
      <div><span class="mono" title="${esc(e.key)}">${esc(name)}</span> <span class="sub">rev ${e.revision}</span></div>
      ${bodyHtml(e.value)}
      <div class="sub">${who(e.updated_by)} · ${stamp(e.updated_at)} · expires in ${countdown(e.expires_in, s.generated_at)}</div>`,
  });

  const walk = (node, path, name, depth) => {
    // A branch leading to one thing, and holding nothing itself, is a longer
    // name rather than a level: `alone/here` is one row, and the reader is
    // spared a heading that organises nothing.
    while (node.kids.size === 1 && !node.entry) {
      const [only] = node.kids.keys();
      name = name + "/" + only;
      path = path + "/" + only;
      node = node.kids.get(only);
    }
    if (node.kids.size === 0) {
      if (node.entry) rows.push(indent(leaf(node.entry, name), depth));
      return;
    }
    const shut = folded.has(path);
    rows.push(twig(path, name + "/", node, depth, shut));
    if (shut) return;
    // A key that is also a branch — `build` written, and `build/ci/unit`
    // written under it — is a child of itself, and reads as one. Named in
    // full, because a row saying `build` directly under `build/` is the one
    // place a tail segment is genuinely ambiguous.
    if (node.entry) rows.push(indent(leaf(node.entry, node.entry.key), depth + 1));
    for (const [kid, child] of [...node.kids.entries()]
        .sort((a, b) => b[1].at - a[1].at)) {
      walk(child, path + "/" + kid, kid, depth + 1);
    }
  };

  for (const [name, node] of [...root.kids.entries()]
      .sort((a, b) => b[1].at - a[1].at)) {
    walk(node, KEYS + name, name, 0);
  }

  const sealed = s.board.filter((e) => e.sealed).sort((a, b) => when(b) - when(a));
  if (sealed.length) {
    const shut = folded.has(SEALED);
    rows.push(twig(SEALED, "🔒 sealed under another key",
                   { n: sealed.length, at: when(sealed[0]) }, 0, shut));
    if (!shut) {
      for (const e of sealed) rows.push(indent(leaf(e, short(e.key)), 1));
    }
  }
  return rows;
}

/** A branch: what is under it, when it last moved, and a way to fold it. */
function twig(path, label, node, depth, shut) {
  return indent({
    key: "twig:" + path,
    cls: "twig",
    html: `<button class="twiglabel" type="button" data-twig="${esc(path)}"
             aria-expanded="${shut ? "false" : "true"}">
        <span class="caret ${shut ? "shut" : ""}" aria-hidden="true">›</span>
        <span class="mono">${esc(label)}</span>
        <span class="sub">${node.n}${node.at
          ? " · " + stamp(new Date(node.at).toISOString()) : ""}</span>
      </button>`,
  }, depth);
}

/** Depth as a class rather than an inline style, because `sync` compares
 *  markup: a row that only moved a level should move, not be rebuilt. */
function indent(row, depth) {
  row.cls = (row.cls ? row.cls + " " : "") + "d" + Math.min(depth, 4);
  return row;
}

//: Paths are namespaced so the sealed branch cannot be collided with by a room
//: that happens to keep a key called "sealed".
const KEYS = "key/";
const SEALED = "sealed";

/** Branches the reader folded away, by path. In memory rather than in storage:
 *  a key is a path in one room and means nothing at all in the next.
 */
const folded = new Set();

export function render(s, { onRoom, onClose } = {}) {
  last = s;
  if (onRoom || onClose) handlers = { onRoom, onClose };
  const { onRoom: room_, onClose: close_ } = handlers;

  $("ws").textContent = s.hub.workspace;
  $("hub").innerHTML = `via <code>${esc(s.hub.url)}</code>` +
    (s.hub.encrypted
      ? ' · <span title="sealed on the wire; this page is where they are opened">encrypted</span>'
      : " · not encrypted");
  // A single failed poll used to flip the room to "hub unreachable" and back,
  // so a flaky connection strobed the header. The caller decides when a hub is
  // really gone; this only paints what it decided.
  if (s.hub.reachable) {
    $("live").className = "dot";
    $("status").textContent = "live";
  } else if (s.stale) {
    $("live").className = "dot stale";
    $("status").textContent = "reconnecting…";
  } else {
    $("live").className = "dot off";
    $("status").textContent = "hub unreachable";
  }
  // The proof-of-room is checked on every refresh and is silent when it passes.
  // Silence is right for a warning and wrong for a fact a reader would like to
  // have, so the pass is a quiet word beside the room rather than nothing.
  const verified = $("verified");
  if (verified) {
    verified.hidden = s.hub.verified !== true;
    verified.title = "an invite's proof-of-room opened here: same hub, same " +
                     "workspace, same key";
  }
  $("stamp").textContent = "updated " + new Date(s.generated_at).toLocaleTimeString();
  $("notes").innerHTML = (s.notes || []).map((n) =>
    `<div class="${/WRONG ROOM/.test(n) ? "fatal" : ""}">${esc(n)}</div>`).join("");

  // One room needs no switcher; several need to say where activity is, or
  // you are picking blind.
  const rooms = s.rooms || [];
  // Where a room came from is worth a glance only when they differ — three
  // tabs all saying "mcp.json" is furniture.
  const mixed = new Set(rooms.map((r) => r.source)).size > 1;
  // A tab can only be closed where closing means something: the browser keeps
  // its rooms and can forget one, but a viewer serving rooms from config would
  // hand the same tab straight back on the next poll.
  $("rooms").innerHTML = rooms.length < 2 ? "" : rooms.map((r) => {
    const state = r.error ? "bad" : (r.awake ? "live" : "");
    const count = r.error ? "unreachable"
      : (r.awake === null ? "—" : `${r.awake} awake`);
    return `<div class="room ${r.selected ? "on" : ""}" data-room="${esc(r.id)}"
              role="tab" tabindex="0" aria-selected="${r.selected ? "true" : "false"}"
              title="${esc(r.workspace)} on ${esc(r.hub)}">
        <span>${esc(r.label)}</span>
        <span class="awake ${state}">${esc(count)}</span>
        ${mixed ? `<span class="where">${esc(r.source)}</span>` : ""}
        ${close_ ? `<button class="close" type="button" data-close="${esc(r.id)}"
              title="Forget ${esc(r.label)}" aria-label="Forget ${esc(r.label)}">×</button>` : ""}
      </div>`;
  }).join("");
  for (const tab of document.querySelectorAll(".room")) {
    const open = () => {
      if (tab.classList.contains("on")) return;
      filter = null;               // channels belong to the room you left
      toBottom = true;             // and so does where you were reading
      if (room_) room_(tab.dataset.room);
    };
    tab.onclick = open;
    tab.onkeydown = (e) => {
      // A div wearing role="tab" has to answer the keys a button would.
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    };
  }
  for (const x of document.querySelectorAll(".room .close")) {
    x.onclick = (e) => {
      e.stopPropagation();         // closing a tab is not switching to it
      close_(x.dataset.close);
    };
    x.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") e.stopPropagation(); };
  }

  // --- channels -------------------------------------------------------------
  //
  // Alphabetical order put the busy channel wherever its name happened to
  // fall, which on a room with a dozen of them means reading the whole row to
  // find where the work is. Most recent first is what a reader is actually
  // after, and `latest_at` was already being carried by both builders and
  // used by neither.
  //
  // Direct messages sort after the shared channels rather than among them: a
  // thread addressed to one agent is a different kind of thing from the room
  // talking, and the conversation already marks it that way.
  const recency = (c) => (c.latest_at ? Date.parse(c.latest_at) : 0);
  const chans = s.channels.slice().sort((a, b) =>
    (a.dm ? 1 : 0) - (b.dm ? 1 : 0) ||
    recency(b) - recency(a) ||
    a.name.localeCompare(b.name));
  if (filter && !chans.some((c) => c.name === filter)) filter = null;
  const here = chans.find((c) => c.name === filter) || null;
  // A sender who has left the roster stops narrowing anything, and a scope you
  // cannot see the edge of is worse than no scope.
  if (sender && !s.agents.some((a) => a.id === sender)) sender = null;
  const speaker = s.agents.find((a) => a.id === sender) || null;

  // Where you are, said in the panel's own heading rather than left to a chip
  // that can be scrolled out of sight on a narrow window. All three parts of
  // the scope, so no combination of them can leave a reader looking at less
  // than they think they are.
  const label = filter === null ? "all channels"
    : (here && !here.named ? "🔒 " + short(filter) : filter);
  const parts = [label];
  if (speaker) parts.push("from " + (speaker.name || speaker.id));
  if (query) parts.push(`matching "${query}"`);
  const narrowed = filter !== null || sender !== null || Boolean(query);
  const head = $("convo-scope");
  head.textContent = parts.join(" · ");
  head.className = narrowed ? "scope on" : "scope";
  $("convo-clear").hidden = !narrowed;

  // Sixty channels is not a switcher, it is a wall. The row keeps what a
  // glance can use and offers the rest, and whatever you are reading is in it
  // however far down the list it sorted.
  const cap = chipCap();
  const capped = chans.length > cap && !allChips;
  const visible = capped
    ? chans.slice(0, cap).concat(
        here && !chans.slice(0, cap).includes(here) ? [here] : [])
    : chans;

  let firstDm = true;
  $("chips").innerHTML =
    [`<button class="chip ${filter === null ? "on" : ""}" data-c="">all<span class="n">${s.messages.length}</span></button>`]
      .concat(visible.map((c) => {
        // One divider, at the seam. Two groups need to be visibly two.
        const seam = c.dm && firstDm;
        if (seam) firstDm = false;
        return (seam ? `<span class="seam" aria-hidden="true"></span>` : "") +
          `<button class="chip ${filter === c.name ? "on" : ""}` +
          `${c.dm ? " dm" : ""}${c.named ? "" : " sealed"}" data-c="${esc(c.name)}"` +
          // A lock and ten characters of token is honest — a channel's name
          // travels inside its messages, so one you cannot open has no name to
          // show — but only if the reason is said where the confusion is,
          // rather than in a banner at the top of the page.
          (c.named ? "" : ` title="this channel's name is sealed under a key ` +
                          `this viewer does not hold"`) +
          `>${c.named ? esc(c.name) : "🔒 " + esc(short(c.name))}` +
          `<span class="n">${c.count}${c.unreadable ? " ⚠" : ""}</span></button>`;
      }))
      .concat(capped
        ? [`<button class="chip more" type="button" data-chips="all">` +
           `+${chans.length - cap} more</button>`]
        : (chans.length > cap
            ? [`<button class="chip more" type="button" data-chips="few">fewer</button>`]
            : []))
      .join("");

  const shown = s.messages.filter(matches);
  const pane = $("messages");
  // Measured before the repaint: afterwards these numbers describe the new
  // content, and "was the reader at the bottom" can no longer be asked.
  const room = `${s.hub.url}/${s.hub.workspace}`;
  const fresh = room !== following;
  // A path folded in one room means nothing in the next: the keys are that
  // room's, and carrying the folds across would hide another room's state
  // behind a decision nobody made about it.
  if (fresh) folded.clear();
  const scopeKey = JSON.stringify(scope());
  const rescoped = scopeKey !== lastScope;
  lastScope = scopeKey;
  const follow = toBottom || fresh || atBottom(pane);
  const keep = pane.scrollTop;
  if (shown.length) {
    const made = sync(pane, shown.map((m) => ({
      key: `${m.token ?? m.channel}#${m.seq}`,
      cls: "msg",
      html: `
      <div class="who">
        <b>${who(m.from)}</b>
        <span class="tag ${m.dm ? "dm" : ""}">${esc(m.channel)}</span>
        ${m.type && m.type !== "note" ? `<span class="tag">${esc(m.type)}</span>` : ""}
        <div>${stamp(m.created_at)}</div>
      </div>
      <div>
        ${m.sealed_body ? `<div class="body sealed">🔒 sealed — this viewer holds no key</div>`
                        : bodyHtml(m.body)}
        ${m.forecast ? `<div class="forecast">next look ~${dur(m.forecast.p50)} (p50)` +
          (m.forecast.speak_p50 ? `, next message ~${dur(m.forecast.speak_p50)}` : "") + `</div>` : ""}
      </div>`,
    })));
    // A message that arrived while you were looking used to be indis-
    // tinguishable from one an hour old: the pane was simply a row longer.
    // One quiet fade says "this is new" without moving anything.
    //
    // Only for messages that actually arrived, though. The first paint of a
    // room is not an arrival — all of it is new then — and neither is widening
    // the scope, which brings back rows the reader themselves filtered away.
    // Six old messages lighting up because a search was cleared says exactly
    // the wrong thing.
    if (painted && !fresh && !rescoped) {
      for (const node of made) {
        node.classList.add("arrived");
        node.addEventListener("animationend",
                              () => node.classList.remove("arrived"), { once: true });
      }
    }
  } else {
    emptyPane(pane, !narrowed ? "nothing said yet"
      : query ? "nothing here matches that"
      : sender ? "nothing from them here"
      : "nothing on this channel");
  }
  painted = true;

  newestSeq = shown.length ? shown[shown.length - 1].seq : 0;
  if (fresh) seenSeq = newestSeq;   // a new room starts caught up
  following = room;
  toBottom = false;

  if (!wired) {
    wired = true;
    // Scrolling back down catches you up immediately rather than at the next
    // refresh, which is the difference between a pill that tracks the reader
    // and one that lingers after they have already read everything.
    pane.addEventListener("scroll", () => {
      if (atBottom(pane)) { seenSeq = newestSeq; drawJump(); }
    }, { passive: true });
    const jump = $("jump");
    if (jump) {
      jump.onclick = () => {
        const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
        pane.scrollTo({ top: pane.scrollHeight, behavior: still ? "auto" : "smooth" });
        seenSeq = newestSeq;
        drawJump();
      };
    }
    // Delegated, because the things that set a filter are now in three places
    // — the chips, the heading, and the channels each agent is watching — and
    // rows that survive a repaint must not be re-wired on every poll.
    document.addEventListener("click", (e) => {
      const pick = e.target.closest("[data-c]");
      if (pick) {
        filter = pick.dataset.c || null;
        // "Show all" is the way out of the whole scope, not out of one third of
        // it: a reader who cleared the channel and was still looking at one
        // agent's messages would have no idea why the room had gone quiet.
        if (pick.id === "convo-clear") { query = ""; sender = null; $("q").value = ""; }
        scopeChanged();
        return;
      }
      const speaker = e.target.closest("[data-from]");
      if (speaker) {
        // Clicking the same name again is the way back out — a filter you can
        // only enter is a trap.
        sender = sender === speaker.dataset.from ? null : speaker.dataset.from;
        scopeChanged();
        return;
      }
      const branch = e.target.closest("[data-twig]");
      if (branch) {
        const path = branch.dataset.twig;
        if (folded.has(path)) folded.delete(path); else folded.add(path);
        if (last) render(last);
        return;
      }
      const chips = e.target.closest("[data-chips]");
      if (chips) {
        allChips = chips.dataset.chips === "all";
        if (last) render(last);
        return;
      }
      const more = e.target.closest("button.more");
      if (more) {
        const box = more.parentElement;
        const open = box.classList.toggle("open");
        more.textContent = open ? "show less" : "show more";
      }
    });
    setInterval(tick, 1000);
  }

  if (s.agents.length) {
    sync($("agents"), s.agents.map((a) => ({
      key: a.id,
      cls: "",
      html: `
      <div><span class="dot ${a.stale ? "stale" : ""}"></span><button
        class="link name ${sender === a.id ? "on" : ""}" type="button"
        data-from="${esc(a.id)}"
        title="${sender === a.id ? "Show everybody again" : "Only what they said"}"
        >${esc(a.name || a.id)}</button></div>
      <div class="sub">${esc(a.kind || "")}${a.branch ? " · " + esc(a.branch) : ""} · seen ${stamp(a.last_seen_at)}</div>
      ${a.task ? `<div class="sub">${esc(a.task)}</div>` : ""}
      ${a.channels.length ? `<div class="sub">watching ` + a.channels.map((c) =>
        // The roster names the channels the chips filter, so it may as well
        // reach them: reading "watching build" and then hunting the row above
        // for a chip that says the same word is work the page can do.
        `<button class="link chanlink" type="button" data-c="${esc(c)}">${esc(c)}</button>`
      ).join(", ") + `</div>` : ""}
      ${a.unreadable ? `<div class="sub warn">a different key</div>` : ""}`,
    })));
  } else {
    emptyPane($("agents"), "nobody is here");
  }

  // Expiry is the one thing on these two panels that is genuinely
  // time-critical, and it was rendered as a number of the same weight whether
  // it said four minutes or four seconds. Soonest first, and the countdown
  // colours itself as it runs out.
  const leases = s.leases.slice().sort((a, b) =>
    (a.expires_in ?? Infinity) - (b.expires_in ?? Infinity));
  if (leases.length) {
    sync($("leases"), leases.map((l) => ({
      key: l.resource,
      cls: "",
      html: `
      <div>${ident(l.resource, l.sealed)}</div>
      <div class="sub">${who(l.holder)} · expires in ${countdown(l.expires_in, s.generated_at)}</div>
      ${l.note ? `<div class="sub">${esc(l.note)}</div>` : ""}`,
    })));
  } else {
    emptyPane($("leases"), "nothing claimed");
  }

  if (s.board.length) {
    sync($("board"), boardRows(s));
  } else {
    emptyPane($("board"), "nothing on the board");
  }

  // Addressed by what they count, not by where they are shown: a narrow window
  // shows the same number twice, once on the panel and once on the switcher
  // that reaches it.
  for (const [what, n] of [["agents", s.agents.length], ["leases", s.leases.length],
                           ["board", s.board.length]]) {
    for (const el of document.querySelectorAll(`[data-count="${what}"]`)) {
      el.textContent = n || "";
    }
  }

  // Before the scroll is settled, not after: a row is inserted with an empty
  // `<time>` in it, and an empty inline element occupies no line box at all.
  // Filling the clocks is therefore what gives every new row its height, and a
  // pane pinned to the bottom before that lands short by exactly the height of
  // the traffic it was supposed to be following.
  tick();
  if (follow) stickToBottom(pane); else pane.scrollTop = keep;
  drawJump();

  $("foot").textContent =
    `switchboard ${s.version} · read-only: this page never posts, and reading here ` +
    `does not advance any agent's cursor.`;
}
