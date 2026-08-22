// Painting a room, wherever the reading was done.
//
// One renderer, two callers: `switchboard_viewer/viewer.py` builds the state in Python
// and serves it over `api/state`; `switchboard_viewer/web/index.html` builds the same
// state in the browser with `switchboard-room.js`. Sharing this file is what
// keeps them one product rather than two that drift — the state shape is the
// contract, and `tests/test_web_page.py` holds the two builders to it.

export let filter = null;   // channel name, or null for everything

export function setFilter(value) { filter = value; toBottom = true; }

// --- following the conversation ---------------------------------------------
//
// The pane is repainted from scratch every few seconds, which by itself sends
// the reader back to the oldest message on every refresh. So the scroll
// position is carried across the repaint, and the rule is the one every chat
// window uses: stay pinned to the newest while the reader is at the bottom,
// and stop the moment they scroll up to read something — a view that yanks
// itself away mid-sentence is worse than one that never moved.
//
// Being scrolled up then has to be survivable, which is what the pill is for:
// it says traffic arrived without moving anything, and clicking it re-pins.

const NEAR_BOTTOM = 40;   // px of slack, so a half-pixel row height still counts

let toBottom = true;      // next paint should land at the newest
let following = null;     // room these scroll numbers belong to
let seenSeq = 0;          // newest seq the reader has actually been shown
let newestSeq = 0;
let wired = false;

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

function ago(iso) {
  if (!iso) return "";
  const secs = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  return dur(secs) + " ago";
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
function bodyHtml(body) {
  if (typeof body === "string") return `<div class="body">${esc(body)}</div>`;
  if (body === null || body === undefined) return `<div class="body sub">(empty)</div>`;
  return `<pre>${esc(JSON.stringify(body, null, 2))}</pre>`;
}

export function render(s, { onRoom, onClose } = {}) {
  $("ws").textContent = s.hub.workspace;
  $("hub").innerHTML = `via <code>${esc(s.hub.url)}</code>` +
    (s.hub.encrypted
      ? ' · <span title="sealed on the wire; this page is where they are opened">encrypted</span>'
      : " · not encrypted");
  $("live").className = "dot" + (s.hub.reachable ? "" : " off");
  $("status").textContent = s.hub.reachable ? "live" : "hub unreachable";
  $("stamp").textContent = "updated " + new Date(s.generated_at).toLocaleTimeString();
  $("notes").innerHTML = (s.notes || []).map((n) => `<div>${esc(n)}</div>`).join("");

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
        ${onClose ? `<button class="close" type="button" data-close="${esc(r.id)}"
              title="Forget ${esc(r.label)}" aria-label="Forget ${esc(r.label)}">×</button>` : ""}
      </div>`;
  }).join("");
  for (const tab of document.querySelectorAll(".room")) {
    const open = () => {
      if (tab.classList.contains("on")) return;
      filter = null;               // channels belong to the room you left
      toBottom = true;             // and so does where you were reading
      if (onRoom) onRoom(tab.dataset.room);
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
      onClose(x.dataset.close);
    };
    x.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") e.stopPropagation(); };
  }

  const chans = s.channels.slice().sort((a, b) => a.name.localeCompare(b.name));
  if (filter && !chans.some((c) => c.name === filter)) filter = null;
  $("chips").innerHTML =
    [`<button class="chip ${filter === null ? "on" : ""}" data-c="">all<span class="n">${s.messages.length}</span></button>`]
      .concat(chans.map((c) =>
        `<button class="chip ${filter === c.name ? "on" : ""}" data-c="${esc(c.name)}">` +
        `${c.named ? esc(c.name) : "🔒 " + esc(short(c.name))}` +
        `<span class="n">${c.count}${c.unreadable ? " ⚠" : ""}</span></button>`)).join("");
  for (const chip of document.querySelectorAll(".chip")) {
    chip.onclick = () => {
      filter = chip.dataset.c || null;
      toBottom = true;   // a different channel starts at its newest, not mid-way
      render(s, { onRoom, onClose });
    };
  }

  const shown = s.messages.filter((m) => filter === null || m.channel === filter);
  const pane = $("messages");
  // Measured before the repaint: afterwards these numbers describe the new
  // content, and "was the reader at the bottom" can no longer be asked.
  const room = `${s.hub.url}/${s.hub.workspace}`;
  const follow = toBottom || room !== following || atBottom(pane);
  const keep = pane.scrollTop;
  pane.innerHTML = shown.length ? shown.map((m) => `
    <div class="msg">
      <div class="who">
        <b>${who(m.from)}</b>
        <span class="tag ${m.dm ? "dm" : ""}">${m.dm ? esc(m.channel) : esc(m.channel)}</span>
        ${m.type && m.type !== "note" ? `<span class="tag">${esc(m.type)}</span>` : ""}
        <div>${esc(ago(m.created_at))}</div>
      </div>
      <div>
        ${m.sealed_body ? `<div class="body sealed">🔒 sealed — this viewer holds no key</div>`
                        : bodyHtml(m.body)}
        ${m.forecast ? `<div class="forecast">next look ~${dur(m.forecast.p50)} (p50)` +
          (m.forecast.speak_p50 ? `, next message ~${dur(m.forecast.speak_p50)}` : "") + `</div>` : ""}
      </div>
    </div>`).join("") : `<div class="empty">nothing on this channel</div>`;

  newestSeq = shown.length ? shown[shown.length - 1].seq : 0;
  if (room !== following) seenSeq = newestSeq;   // a new room starts caught up
  following = room;
  toBottom = false;
  if (follow) stickToBottom(pane); else pane.scrollTop = keep;
  drawJump();

  if (!wired) {
    // Scrolling back down catches you up immediately rather than at the next
    // refresh, which is the difference between a pill that tracks the reader
    // and one that lingers after they have already read everything.
    wired = true;
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
  }

  $("agents").innerHTML = s.agents.length ? s.agents.map((a) => `
    <div>
      <div><span class="dot ${a.stale ? "stale" : ""}"></span><span class="name">${esc(a.name || a.id)}</span></div>
      <div class="sub">${esc(a.kind || "")}${a.branch ? " · " + esc(a.branch) : ""} · seen ${esc(ago(a.last_seen_at))}</div>
      ${a.task ? `<div class="sub">${esc(a.task)}</div>` : ""}
      ${a.channels.length ? `<div class="sub">watching ${a.channels.map(esc).join(", ")}</div>` : ""}
      ${a.unreadable ? `<div class="sub" style="color:var(--warn)">a different key</div>` : ""}
    </div>`).join("") : `<div class="empty">nobody is here</div>`;

  $("leases").innerHTML = s.leases.length ? s.leases.map((l) => `
    <div>
      <div>${ident(l.resource, l.sealed)}</div>
      <div class="sub">${who(l.holder)} · expires in ${dur(l.expires_in)}</div>
      ${l.note ? `<div class="sub">${esc(l.note)}</div>` : ""}
    </div>`).join("") : `<div class="empty">nothing claimed</div>`;

  $("board").innerHTML = s.board.length ? s.board.map((e) => `
    <div>
      <div>${ident(e.key, e.sealed)} <span class="sub">rev ${e.revision}</span></div>
      ${bodyHtml(e.value)}
      <div class="sub">${who(e.updated_by)} · expires in ${dur(e.expires_in)}</div>
    </div>`).join("") : `<div class="empty">nothing on the board</div>`;

  $("foot").textContent =
    `switchboard ${s.version} · read-only: this page never posts, and reading here ` +
    `does not advance any agent's cursor.`;
}
