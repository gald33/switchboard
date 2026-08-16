// Painting a room, wherever the reading was done.
//
// One renderer, two callers: `examples/viewer.py` builds the state in Python
// and serves it over `api/state`; `examples/web/index.html` builds the same
// state in the browser with `switchboard-room.js`. Sharing this file is what
// keeps them one product rather than two that drift — the state shape is the
// contract, and `tests/test_web_page.py` holds the two builders to it.

export let filter = null;   // channel name, or null for everything

export function setFilter(value) { filter = value; }

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

export function render(s, { onRoom } = {}) {
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
  $("rooms").innerHTML = rooms.length < 2 ? "" : rooms.map((r) => {
    const state = r.error ? "bad" : (r.awake ? "live" : "");
    const count = r.error ? "unreachable"
      : (r.awake === null ? "—" : `${r.awake} awake`);
    return `<button class="room ${r.selected ? "on" : ""}" data-room="${esc(r.id)}"
              title="${esc(r.workspace)} on ${esc(r.hub)}">
        <span>${esc(r.label)}</span>
        <span class="awake ${state}">${esc(count)}</span>
        ${mixed ? `<span class="where">${esc(r.source)}</span>` : ""}
      </button>`;
  }).join("");
  for (const tab of document.querySelectorAll(".room")) {
    tab.onclick = () => {
      if (tab.classList.contains("on")) return;
      filter = null;               // channels belong to the room you left
      if (onRoom) onRoom(tab.dataset.room);
    };
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
    chip.onclick = () => { filter = chip.dataset.c || null; render(s, { onRoom }); };
  }

  const shown = s.messages.filter((m) => filter === null || m.channel === filter);
  $("messages").innerHTML = shown.length ? shown.map((m) => `
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
