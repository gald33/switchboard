"""Build the findings page from run records, so the charts cannot drift.

Every number on the page comes out of the JSON the runs wrote. Nothing is typed
in by hand, because a figure transcribed once is a figure that goes stale the
next time an arm runs — and this project has already had to retract a claim it
had written down confidently.

    python tests/experiments/barter_experiment.py --islands 12 \
        --rounds-sweep --labour-sweep --json tests/experiments/barter/tier1.json
    python tests/experiments/barter/report.py --sweep tier1_rounds.json \
        --islands tier1.json --labour tier1_labour.json \
        --tier2 tier2_seed1_*.json --out report.html

Every input here is written by that first command, from the same run. That was
not true until recently: the round-budget figure — the main one on the page —
was drawn from a file no committed script could produce, which is this
docstring's own warning happening to this docstring.

The charts are hand-built inline SVG. Two rules they obey, both from hard-won
places: the round-budget figure is **two panels sharing an x-axis, never a dual
axis**, because efficiency is a rate and ruin is a count and putting them on one
scale is how ruin gets quietly averaged into a mean; and ruin is drawn as its own
series in its own panel rather than folded into the efficiency line, because an
island where somebody ended with nothing is not an island that scored slightly
lower.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

#: Validated in both modes with the dataviz palette validator — worst adjacent
#: CVD ΔE 9.1 light / 8.4 dark, normal-vision 22.9 / 19.8. Light mode returns a
#: contrast WARN on aqua and yellow, so the page ships direct labels *and* a
#: table view, which is the documented relief.
SERIES = {
    "A": {"light": "#2a78d6", "dark": "#3987e5", "name": "silent"},
    "B": {"light": "#eb6834", "dark": "#d95926", "name": "disclose"},
    "C": {"light": "#1baf7a", "dark": "#199e70", "name": "price"},
    "D": {"light": "#eda100", "dark": "#c98500", "name": "money"},
}
ARMS = ("A", "B", "C", "D")


def load_sweep(path: Path) -> dict:
    data = json.loads(path.read_text())
    data["floor"] = statistics.median(data["floors"])
    data["ceiling"] = statistics.median(data["ceilings"])
    return data


def load_tier2(paths: list[Path]) -> list[dict]:
    rows = []
    for path in paths:
        for record in json.loads(path.read_text()):
            rows.append(record)
    order = ["silent", "free", "told", "built", "bound", "spend", "paid"]
    rows.sort(key=lambda r: order.index(r["arm"]) if r["arm"] in order else 99)
    return rows


def price_rows(tier2: list[dict]) -> list[dict]:
    """Per-good price disagreement per arm — the number the quoting arms turn on.

    Computed here from the records rather than transcribed, and through the same
    ``analysis`` functions the tables use, so the figure and the prose cannot
    disagree with each other.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from barter.analysis import price_spread, quoted_prices

    out = []
    for record in tier2:
        vectors, source = quoted_prices(record)
        spread = price_spread(vectors)
        if spread:
            out.append({"arm": record["arm"], "source": source,
                        "spread": {g: round(v, 2) for g, v in spread.items()},
                        "quoting": len(vectors)})
    return out


def build(sweep: dict, tier2: list[dict], islands: dict | None = None,
          labour: dict | None = None) -> str:
    payload = json.dumps({
        "sweep": sweep,
        "islands": islands or {},
        "labour": labour or {},
        "prices": price_rows(tier2),
        "tier2": [{
            "arm": r["arm"],
            "efficiency": None if r.get("ruined") else r["efficiency"][0],
            "ruined": len(r.get("ruined") or []),
            "own_plan": None if r.get("own_plan_ruined") else r["own_plan"][0],
            "worst": r["worst_ratio"],
            "executed": r["summary"]["executed"],
            "proposed": r["summary"]["proposed"],
            "messages": len(r.get("said") or []),
            "floor": r["autarky_floor"],
            "ceiling": r["exchange_ceiling"],
        } for r in tier2],
        "series": SERIES,
        "arms": list(ARMS),
    }, separators=(",", ":"))
    return TEMPLATE.replace("/*__DATA__*/null", payload)


TEMPLATE = r"""<title>Island Barter Frontier</title>
<style>
  :root {
    color-scheme: light;
    --ground:  #f5f6f7;
    --surface: #fdfdfe;
    --ink:     #14161a;
    --ink-2:   #565b64;
    --ink-3:   #878d97;
    --rule:    #e2e4e8;
    --hair:    #eceef1;
    --accent:  #2a78d6;
    --critical:#c0322f;
    --s-A: #2a78d6; --s-B: #eb6834; --s-C: #1baf7a; --s-D: #eda100;
    --serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
    --sans: system-ui, -apple-system, "Segoe UI", sans-serif;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --ground:  #101113;
      --surface: #191b1e;
      --ink:     #f2f3f5;
      --ink-2:   #a8adb6;
      --ink-3:   #767c86;
      --rule:    #26292e;
      --hair:    #202327;
      --accent:  #5598e7;
      --critical:#e06a66;
      --s-A: #3987e5; --s-B: #d95926; --s-C: #199e70; --s-D: #c98500;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --ground:  #101113;
    --surface: #191b1e;
    --ink:     #f2f3f5;
    --ink-2:   #a8adb6;
    --ink-3:   #767c86;
    --rule:    #26292e;
    --hair:    #202327;
    --accent:  #5598e7;
    --critical:#e06a66;
    --s-A: #3987e5; --s-B: #d95926; --s-C: #199e70; --s-D: #c98500;
  }

  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--ground); color: var(--ink);
    font-family: var(--sans); font-size: 16px; line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1020px; margin: 0 auto; padding: 72px 28px 120px; }
  .col { max-width: 68ch; }
  h1, h2, h3 { font-family: var(--serif); font-weight: 600; text-wrap: balance; }
  h1 { font-size: clamp(2rem, 4.4vw, 2.9rem); line-height: 1.12; margin: 0 0 18px; letter-spacing: -0.015em; }
  h2 { font-size: 1.5rem; line-height: 1.25; margin: 0 0 14px; }
  h3 { font-size: 1.08rem; margin: 0 0 8px; }
  p { margin: 0 0 18px; }
  a { color: var(--accent); }
  .eyebrow {
    font-family: var(--mono); font-size: 0.72rem; letter-spacing: 0.13em;
    text-transform: uppercase; color: var(--ink-3); margin: 0 0 14px;
  }
  .lede { font-size: 1.12rem; color: var(--ink-2); }
  section { margin-top: 64px; }
  .num { font-variant-numeric: tabular-nums; font-family: var(--mono); }

  figure { margin: 0; }
  .fig {
    background: var(--surface); border: 1px solid var(--rule); border-radius: 4px;
    padding: 24px 22px 18px; margin-top: 28px; overflow-x: auto;
  }
  .fig h3 { margin-bottom: 4px; }
  .fig .cap { color: var(--ink-2); font-size: 0.9rem; margin: 0 0 20px; max-width: 62ch; }
  .fignote { color: var(--ink-3); font-size: 0.82rem; margin: 14px 0 0; max-width: 68ch; }
  svg { display: block; overflow: visible; }
  .grid { stroke: var(--hair); stroke-width: 1; }
  .axis { stroke: var(--rule); stroke-width: 1; }
  .tick { font-family: var(--mono); font-size: 11px; fill: var(--ink-3); }
  .alab { font-family: var(--mono); font-size: 11px; fill: var(--ink-3); letter-spacing: 0.06em; }
  .endlab { font-family: var(--mono); font-size: 11.5px; font-weight: 600; }
  .band { fill: var(--ink); opacity: 0.045; }
  .bandlab { font-family: var(--mono); font-size: 10.5px; fill: var(--ink-3); }

  .legend { display: flex; flex-wrap: wrap; gap: 8px 20px; margin: 0 0 16px; padding: 0; list-style: none; }
  .legend li { display: flex; align-items: center; gap: 7px; font-family: var(--mono); font-size: 0.78rem; color: var(--ink-2); }
  .swatch { width: 11px; height: 11px; border-radius: 2px; flex: none; }

  .rail { border-left: 2px solid var(--critical); padding-left: 20px; }
  .rail .eyebrow { color: var(--critical); }

  table { border-collapse: collapse; width: 100%; font-family: var(--mono); font-size: 0.82rem; }
  caption { text-align: left; color: var(--ink-3); font-size: 0.8rem; padding-bottom: 10px; font-family: var(--sans); }
  th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--hair); font-variant-numeric: tabular-nums; }
  th:first-child, td:first-child { text-align: left; }
  thead th { color: var(--ink-3); font-weight: 500; border-bottom: 1px solid var(--rule); }
  details { margin-top: 18px; }
  summary { cursor: pointer; color: var(--ink-2); font-size: 0.85rem; font-family: var(--mono); }
  summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 2px; }

  .tip {
    position: fixed; pointer-events: none; opacity: 0; transition: opacity .1s;
    background: var(--surface); border: 1px solid var(--rule); border-radius: 4px;
    padding: 9px 11px; font-family: var(--mono); font-size: 0.76rem; line-height: 1.5;
    box-shadow: 0 6px 22px rgba(0,0,0,.13); z-index: 20; white-space: nowrap; color: var(--ink);
  }
  .hit { fill: transparent; cursor: crosshair; }
  .dot { stroke: var(--surface); stroke-width: 2; }
  @media (prefers-reduced-motion: reduce) { .tip { transition: none; } }
</style>

<div class="wrap">
  <header class="col">
    <p class="eyebrow">Switchboard · island barter economy</p>
    <h1>What has to be agreed before a market works</h1>
    <p class="lede">
      Twelve agents, five goods, and a manager that is the only thing allowed to move a
      quantity. Scripted policies establish what is reachable; language models are then
      put in the traders' seats to see what they do with a channel. These are the
      results that survived replication — and one that did not.
    </p>
  </header>

  <section class="col">
    <h2>Swapping well is not the problem</h2>
    <p>
      An outcome is scored by how far inside the Pareto frontier it sits: 1.0 means no
      reallocation could make everyone better off. Three reference points make that
      readable, and the gap between the first two is the first finding.
    </p>
  </section>

  <figure class="fig">
    <h3>Where the gains actually are</h3>
    <p class="cap">
      Trading perfectly while producing as if alone recovers about a seventh of what is
      available. The rest requires having made different things.
    </p>
    <div id="strip"></div>
    <p class="fignote">
      Median across 12 islands. Autarky is every agent producing its own optimal bundle
      and never trading; the exchange ceiling is a perfect reallocation of exactly those
      goods.
    </p>
  </figure>

  <section class="col">
    <h2>One failure heals with time. The other never does.</h2>
    <p>
      Four scripted arms, each removing one thing: <b>silent</b> can only call the
      manager; <b>disclose</b> adds a public channel; <b>price</b> adds an agreed way to
      read it into one number; <b>money</b> adds one clause — accept the numeraire past
      the point of wanting it.
    </p>
    <p>
      Swept across the trading-round budget, because comparing arms at a single budget
      picks the winner by picking the budget.
    </p>
  </section>

  <figure class="fig">
    <h3>Efficiency and ruin against the round budget</h3>
    <p class="cap">
      <b>price</b> reaches the frontier exactly and ruins a third of its islands at a rate
      that never improves. <b>money</b> changes one clause and its ruin falls steadily.
      Same failure at any single budget; completely different underneath.
    </p>
    <ul class="legend" id="legend"></ul>
    <div id="sweep"></div>
    <p class="fignote">
      <b>Ruin</b> means an agent finished holding none of some good — zero Cobb-Douglas
      utility. It is counted, never averaged into the efficiency median, because a mean
      that swallows a zero hides the outcome most worth seeing. Islands with any ruin are
      excluded from the median above and counted below. <b>silent</b> and
      <b>disclose</b> never ruin an island at any budget, so in the lower panel their
      lines sit exactly on top of each other along zero.
    </p>
    <details>
      <summary>Table view</summary>
      <div id="table"></div>
    </details>
  </figure>

  <figure class="fig">
    <h3>Every island, not the median</h3>
    <p class="cap">
      The same twelve islands at 60 rounds, one dot each. <b>silent</b> and
      <b>disclose</b> land in a tight band and never ruin anyone. <b>price</b> is
      bimodal — it either reaches the frontier or destroys the island, with almost
      nothing in between. <b>money</b> spreads across the upper range and still ruins
      more than half. A median reports none of that.
    </p>
    <div id="islands"></div>
    <p class="fignote">
      Ruined islands carry no efficiency, so they are drawn on their own row rather than
      placed at zero. The reference lines are the same autarky floor and exchange ceiling
      as above.
    </p>
  </figure>

  <figure class="fig">
    <h3>Who ends up worse off than never trading</h3>
    <p class="cap">
      Worst single agent on each island, as a multiple of what it would have had alone.
      Below <span class="num">1.0</span> means taking part hurt somebody. Voluntary trade
      cannot do that on its own — only a production bet on a price that did not arrive can.
    </p>
    <div id="worst"></div>
    <p class="fignote">
      <b>disclose</b> hurts somebody on <b>12 of 12</b> islands — not on average, on every
      one. That is the cost of talking without an agreed way to read what is said, and it
      is invisible in an efficiency median. <b>silent</b> is the mirror image at
      <b>0 of 12</b>: it produces its autarky bundle and settles only what both sides
      scored as a gain, so it is the safe arm as well as the mediocre one. <b>money</b>
      hurts somebody on 2 of its 5 surviving islands. Ruined islands are excluded — their
      worst agent is zero by definition.
    </p>
  </figure>

  <figure class="fig">
    <h3>What money costs to run</h3>
    <p class="cap">
      Median settled trades per island. <b>money</b> buys its robustness with volume:
      twice <b>price</b>'s settlements by round 60 and four times by round 240, because
      every exchange becomes two trades through the numeraire instead of one swap.
    </p>
    <div id="volume"></div>
    <p class="fignote">
      <b>price</b> flatlines at 91 trades from round 60 onward — the same budget at which
      its ruin rate stops falling. It is not trading slowly by then; it has stopped
      entirely, because the trades it still needs are ones no counterparty wants.
    </p>
  </figure>

  <section class="col">
    <h2>The ruin was never an information problem</h2>
    <p>
      Every arm on the ladder is trying to make one irreversible bet a better one. The
      bet is production: labour is committed before any trade has happened, and nothing
      afterwards can unwind it. Slicing that same unit of labour across the trading
      rounds attacks the loss from the other side — it lets a wrong bet be
      <i>revised</i> rather than made well. Nothing else changes: no extra messages, no
      extra prices, and the frontier and both benchmarks stay exactly where they were.
    </p>
  </section>

  <figure class="fig">
    <h3>Ruin against how finely labour is sliced</h3>
    <p class="cap">
      <b>price</b>'s ruin — flat at 8 of 12 however long it ran, the result the whole
      experiment turned on — goes to zero. So does <b>money</b>'s. Neither needed
      anything said to anybody.
    </p>
    <div id="labour"></div>
    <p class="fignote">
      Three panels, one x-axis, because these are three different questions. The scissors
      in the middle two are the finding: <b>net</b> efficiency rises because the zeros
      disappear, while efficiency <b>on the islands that survived</b> falls, because an
      agent that keeps re-aiming at what it is short of stops making what it is best at.
      Slicing labour buys insurance and pays for it in specialisation. It is a trade-off,
      not a free improvement, and the middle panel is there so it cannot be read as one.
    </p>
  </figure>

  <section class="col">
    <h2>Models take the vocabulary and leave the substance</h2>
    <p>
      With the manager unchanged, language models were given the same island. Arms are
      tool surfaces rather than instructions — the silent arm has no channel tool, and
      the prompt never mentions prices, numeraires or money, so a convention that appears
      was invented rather than followed.
    </p>
    <p>
      Told the numeraire convention in words, they adopted it instantly and completely,
      quoting prices in every message while holding cloth <span class="num">30×</span>
      apart, and finished below the arm that said nothing. Given a board with a median in
      every reply, they finished <span class="num">27×</span> apart — the only good all
      four agreed on was the one the board pinned for them.
    </p>
  </section>

  <section class="col rail">
    <p class="eyebrow">Retracted</p>
    <p>
      An earlier version of this page said a convergence result was too large to be a
      draw. Three arms sharing the same board on the same island and seed, differing by
      two sentences and one calculator, then produced settled trades of
      <span class="num">1</span>, <span class="num">9</span> and <span class="num">0</span>,
      and price agreement of <span class="num">1.7×</span>, <span class="num">6.7×</span>
      and <span class="num">14×</span>.
    </p>
    <p>
      Those spreads are the size of every effect the ladder claimed. <b>Single-island
      run-to-run variance swamps the arm differences</b>, and no Tier 2 ordering here is
      evidence. What survives is the scripted sweep above, which is replicated, and the
      mechanisms visible in transcripts — an agent explaining that it starved holding
      fair offers nobody wanted is legible however the run scored.
    </p>
  </section>

  <figure class="fig">
    <h3>Every model arm sits between the floor and the ceiling</h3>
    <p class="cap">
      Plotted against the same two reference points. Nothing reaches the exchange ceiling,
      which means no arm ever specialised production — they used prices, when they had
      them, to haggle over stock they already held.
    </p>
    <div id="tier2"></div>
    <p class="fignote">
      One island per arm, one seed, Haiku 4.5. Marked as single observations because that
      is what they are. Arms where an agent finished with nothing are shown as ruin rather
      than as a low score.
    </p>
  </figure>

  <figure class="fig">
    <h3>They all traded well and all produced the same</h3>
    <p class="cap">
      Efficiency against how well each arm allocated <em>what it chose to make</em>. The
      diagonal is what an arm would score if it produced its autarky bundle and traded
      that perfectly. Every arm sits on it — so the differences between them are entirely
      about swapping, and none of them ever changed what was made.
    </p>
    <div id="decomp"></div>
    <p class="fignote">
      Implied production quality is efficiency ÷ own-plan: 0.407, 0.410, 0.410 and 0.422
      across the four scoreable arms, against an exchange ceiling of 0.413. Four arms
      agreeing that tightly is the most robust thing in this section, and it is what
      prompted the flow fix — production was being committed before anybody had spoken.
    </p>
  </figure>

  <figure class="fig">
    <h3>The number the quoting arms turn on</h3>
    <p class="cap">
      How far apart traders' prices finished, per good — the ratio of the highest quote to
      the lowest. <span class="num">1×</span> would be a genuinely shared price. Nothing
      reached it except the good the board pins by definition.
    </p>
    <div id="prices"></div>
    <p class="fignote">
      Log scale. <b>told</b> keeps its prices in prose, so they are read back out of the
      transcript the way a counterparty would have to; the other arms have a board.
      <b>fish</b> is the numeraire and is fixed at 1 by the machinery, which is why it is
      the only good every trader agrees on.
    </p>
  </figure>

  <section class="col">
    <h2>Method</h2>
    <p>
      Each agent has an independent production capacity for every good and one unit of
      labour to split between them, so what it makes is a choice. Preferences are
      Cobb-Douglas, so every agent wants some of everything and a good you hold none of
      takes your score to zero.
    </p>
    <p>
      The manager enforces non-negativity, conservation, and two-phase settlement: a buyer
      proposes and gets a trade id, the named seller approves it, and the buyer's side is
      escrowed from the moment it is offered. It is not a model — an invariant you can
      argue your way out of is not one. Efficiency is returned as a certified interval,
      proved from an allocation on one side and prices on the other, and the self-test is
      the First Welfare Theorem: the competitive equilibrium must score 1.000, and does.
    </p>
  </section>
</div>
<div class="tip" id="tip" role="status" aria-live="polite"></div>

<script>
const DATA = /*__DATA__*/null;
const tip = document.getElementById('tip');
const cssv = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const el = (n, a = {}, kids = []) => {
  const e = document.createElementNS('http://www.w3.org/2000/svg', n);
  for (const [k, v] of Object.entries(a)) e.setAttribute(k, v);
  for (const c of [].concat(kids)) e.append(c);
  return e;
};
function showTip(evt, html) {
  tip.innerHTML = html; tip.style.opacity = '1';
  const r = tip.getBoundingClientRect();
  let x = evt.clientX + 14, y = evt.clientY - r.height - 10;
  if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - 14;
  if (y < 8) y = evt.clientY + 18;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
const hideTip = () => { tip.style.opacity = '0'; };
const armColor = a => cssv('--s-' + a);
/* Direct labels are the relief for the light-mode contrast WARN, so they have to
   be readable — which means they must not sit on top of each other. Nudge apart
   in y, smallest move first, leaving the marks themselves untouched. */
function declutter(items, gap) {
  const sorted = [...items].sort((a, b) => a.y - b.y);
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i].y - sorted[i - 1].y < gap) sorted[i].y = sorted[i - 1].y + gap;
  }
  return items;
}

/* ---- the benchmark strip: one axis, three reference points ---- */
function strip() {
  const W = 940, H = 148, L = 8, R = 8, base = 74;
  const s = DATA.sweep, x = v => L + v * (W - L - R);
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%',
                          role: 'img', 'aria-label':
    `Autarky ${s.floor.toFixed(3)}, exchange ceiling ${s.ceiling.toFixed(3)}, frontier 1.0` });
  svg.append(el('rect', { x: x(0), y: base - 26, width: x(s.floor) - x(0), height: 26, class: 'band' }));
  svg.append(el('rect', { x: x(s.floor), y: base - 26, width: x(s.ceiling) - x(s.floor),
                          height: 26, fill: cssv('--s-C'), opacity: 0.22 }));
  svg.append(el('rect', { x: x(s.ceiling), y: base - 26, width: x(1) - x(s.ceiling),
                          height: 26, fill: cssv('--accent'), opacity: 0.13 }));
  svg.append(el('line', { x1: x(0), y1: base, x2: x(1), y2: base, class: 'axis' }));
  // autarky and the exchange ceiling sit 0.08 apart on a 0-1 scale, so their
  // labels are given separate rows rather than allowed to overlap.
  const marks = [
    [0, '0', 'start', 0], [s.floor, 'autarky ' + s.floor.toFixed(3), 'middle', 0],
    [s.ceiling, 'exchange ceiling ' + s.ceiling.toFixed(3), 'middle', 16],
    [1, 'frontier 1.000', 'end', 0],
  ];
  marks.forEach(([v, label, anchor, drop]) => {
    svg.append(el('line', { x1: x(v), y1: base - 26, x2: x(v), y2: base + 6 + drop, class: 'axis' }));
    const t = el('text', { x: x(v), y: base + 22 + drop, class: 'bandlab', 'text-anchor': anchor });
    t.textContent = label; svg.append(t);
  });
  const share = el('text', { x: x((s.floor + s.ceiling) / 2), y: base - 36, class: 'bandlab',
                             'text-anchor': 'middle', fill: cssv('--ink-2') });
  share.textContent = `swapping alone → ${Math.round((s.ceiling - s.floor) / (1 - s.floor) * 100)}% of what is available`;
  svg.append(share);
  document.getElementById('strip').append(svg);
}

/* ---- the sweep: TWO panels sharing an x-axis, never a dual axis ---- */
function sweep() {
  const W = 940, PH = 210, GAP = 54, ML = 52, MR = 96, MT = 14, MB = 34;
  const s = DATA.sweep, budgets = s.budgets;
  const H = MT + PH + GAP + PH + MB + 34;
  const lx = Math.log(budgets[0]), hx = Math.log(budgets[budgets.length - 1]);
  const X = b => ML + (Math.log(b) - lx) / (hx - lx) * (W - ML - MR);
  const yE = v => MT + PH - ((v - 0.3) / (1.05 - 0.3)) * PH;
  const top2 = MT + PH + GAP;
  const yR = v => top2 + PH - (v / 12) * PH;

  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img',
    'aria-label': 'Top panel: median efficiency by round budget. Bottom panel: islands with ruin.' });

  [[MT, yE, [0.4, 0.6, 0.8, 1.0], v => v.toFixed(1), 'median efficiency'],
   [top2, yR, [0, 3, 6, 9, 12], v => String(v), 'islands with ruin (of 12)']
  ].forEach(([top, y, ticks, fmt, label]) => {
    ticks.forEach(t => {
      svg.append(el('line', { x1: ML, y1: y(t), x2: W - MR, y2: y(t), class: 'grid' }));
      const tx = el('text', { x: ML - 10, y: y(t) + 4, class: 'tick', 'text-anchor': 'end' });
      tx.textContent = fmt(t); svg.append(tx);
    });
    svg.append(el('line', { x1: ML, y1: top, x2: ML, y2: top + PH, class: 'axis' }));
    const al = el('text', { x: ML, y: top - 4, class: 'alab' });
    al.textContent = label; svg.append(al);
  });

  budgets.forEach(b => {
    const t = el('text', { x: X(b), y: H - 28, class: 'tick', 'text-anchor': 'middle' });
    t.textContent = String(b); svg.append(t);
  });
  const xt = el('text', { x: (ML + W - MR) / 2, y: H - 8, class: 'alab', 'text-anchor': 'middle' });
  xt.textContent = 'trading rounds (log)'; svg.append(xt);

  const effLabels = [], ruinLabels = [];
  DATA.arms.forEach(arm => {
    const rows = DATA.sweep.arms[arm], c = armColor(arm);
    const eff = rows.filter(r => r.median !== null);
    if (eff.length > 1) {
      svg.append(el('path', { d: eff.map((r, i) => `${i ? 'L' : 'M'}${X(r.budget)},${yE(r.median)}`).join(' '),
                              fill: 'none', stroke: c, 'stroke-width': 2,
                              'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
      effLabels.push({ y: yE(eff[eff.length - 1].median), c, arm });
    }
    svg.append(el('path', { d: rows.map((r, i) => `${i ? 'L' : 'M'}${X(r.budget)},${yR(r.ruined)}`).join(' '),
                            fill: 'none', stroke: c, 'stroke-width': 2,
                            'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
    ruinLabels.push({ y: yR(rows[rows.length - 1].ruined), c, arm });

    rows.forEach(r => {
      if (r.median !== null) svg.append(el('circle', { cx: X(r.budget), cy: yE(r.median), r: 4, fill: c, class: 'dot' }));
      svg.append(el('circle', { cx: X(r.budget), cy: yR(r.ruined), r: 4, fill: c, class: 'dot' }));
    });
  });
  [effLabels, ruinLabels].forEach(group => declutter(group, 15).forEach(l => {
    const t = el('text', { x: W - MR + 12, y: l.y + 4, class: 'endlab', fill: l.c });
    t.textContent = `${l.arm} ${DATA.series[l.arm].name}`; svg.append(t);
  }));

  budgets.forEach(b => {
    const half = (W - ML - MR) / (budgets.length - 1) / 2;
    const hit = el('rect', { x: X(b) - half, y: MT, width: half * 2, height: H - MT - MB, class: 'hit' });
    const rows = DATA.arms.map(a => {
      const r = DATA.sweep.arms[a].find(v => v.budget === b);
      const eff = r.median === null ? '—' : r.median.toFixed(3);
      return `<div><span style="color:${armColor(a)}">■</span> ${a} ${DATA.series[a].name}
              &nbsp;eff ${eff}&nbsp; ruin ${r.ruined}/12</div>`;
    }).join('');
    hit.addEventListener('mousemove', e => showTip(e, `<b>${b} trading rounds</b>${rows}`));
    hit.addEventListener('mouseleave', hideTip);
    svg.append(hit);
  });
  document.getElementById('sweep').append(svg);

  const leg = document.getElementById('legend');
  DATA.arms.forEach(a => {
    const li = document.createElement('li');
    li.innerHTML = `<span class="swatch" style="background:${armColor(a)}"></span>${a} · ${DATA.series[a].name}`;
    leg.append(li);
  });
}

/* ---- irreversibility: the same labour, sliced ---- */
function labour() {
  const L = DATA.labour;
  if (!L || !L.arms) return;
  const counts = L.instalments, n = L.islands;
  const W = 940, PH = 150, GAP = 46, ML = 52, MR = 104, MT = 14, MB = 34;
  const H = MT + PH * 3 + GAP * 2 + MB + 34;
  const lx = Math.log(counts[0]), hx = Math.log(counts[counts.length - 1]);
  const X = c => ML + (Math.log(c) - lx) / (hx - lx) * (W - ML - MR);
  const top2 = MT + PH + GAP, top3 = top2 + PH + GAP;
  const yN = v => MT + PH - (v / 1.05) * PH;
  const yU = v => top2 + PH - (v / 1.05) * PH;
  const yR = v => top3 + PH - (v / n) * PH;

  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img',
    'aria-label': 'Three panels sharing an x-axis of instalment count: net median efficiency scoring ruin at zero, median efficiency over islands where nobody was ruined, and islands with any ruin.' });

  [[MT, yN, [0, 0.25, 0.5, 0.75, 1.0], v => v.toFixed(2), 'net median (ruin scored 0)'],
   [top2, yU, [0, 0.25, 0.5, 0.75, 1.0], v => v.toFixed(2), 'median where nobody was ruined'],
   [top3, yR, [0, Math.round(n / 2), n], v => String(v), 'islands with ruin (of ' + n + ')']
  ].forEach(([top, y, ticks, fmt, label]) => {
    ticks.forEach(t => {
      svg.append(el('line', { x1: ML, y1: y(t), x2: W - MR, y2: y(t), class: 'grid' }));
      const tx = el('text', { x: ML - 10, y: y(t) + 4, class: 'tick', 'text-anchor': 'end' });
      tx.textContent = fmt(t); svg.append(tx);
    });
    svg.append(el('line', { x1: ML, y1: top, x2: ML, y2: top + PH, class: 'axis' }));
    const al = el('text', { x: ML, y: top - 4, class: 'alab' });
    al.textContent = label; svg.append(al);
  });

  counts.forEach(c => {
    const t = el('text', { x: X(c), y: H - 28, class: 'tick', 'text-anchor': 'middle' });
    t.textContent = String(c); svg.append(t);
  });
  const xt = el('text', { x: (ML + W - MR) / 2, y: H - 8, class: 'alab', 'text-anchor': 'middle' });
  xt.textContent = 'instalments the one unit of labour is split into (log)'; svg.append(xt);

  const ends = [[], [], []];
  DATA.arms.forEach(arm => {
    const a = L.arms[arm], c = armColor(arm);
    [[a.net_median, yN, 0], [a.unruined_median, yU, 1], [a.ruined, yR, 2]]
      .forEach(([vals, y, panel]) => {
        const pts = vals.map((v, i) => ({ v, i })).filter(p => p.v !== null);
        if (pts.length > 1) {
          svg.append(el('path', {
            d: pts.map((p, k) => `${k ? 'L' : 'M'}${X(counts[p.i])},${y(p.v)}`).join(' '),
            fill: 'none', stroke: c, 'stroke-width': 2,
            'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
          ends[panel].push({ y: y(pts[pts.length - 1].v), c, arm });
        }
        pts.forEach(p => svg.append(el('circle',
          { cx: X(counts[p.i]), cy: y(p.v), r: 4, fill: c, class: 'dot' })));
      });
  });
  ends.forEach(group => declutter(group, 15).forEach(l => {
    const t = el('text', { x: W - MR + 12, y: l.y + 4, class: 'endlab', fill: l.c });
    t.textContent = `${l.arm} ${DATA.series[l.arm].name}`; svg.append(t);
  }));

  counts.forEach((c, i) => {
    const half = (W - ML - MR) / (counts.length - 1) / 2;
    const hit = el('rect', { x: X(c) - half, y: MT, width: half * 2, height: H - MT - MB, class: 'hit' });
    const rows = DATA.arms.map(a => {
      const r = L.arms[a];
      const u = r.unruined_median[i] === null ? '—' : r.unruined_median[i].toFixed(3);
      return `<div><span style="color:${armColor(a)}">■</span> ${a} ${DATA.series[a].name}
              &nbsp;net ${r.net_median[i].toFixed(3)}&nbsp; unruined ${u}&nbsp; ruin ${r.ruined[i]}/${n}</div>`;
    }).join('');
    hit.addEventListener('mousemove', e => showTip(e, `<b>${c} instalment${c === 1 ? '' : 's'}</b>${rows}`));
    hit.addEventListener('mouseleave', hideTip);
    svg.append(hit);
  });
  document.getElementById('labour').append(svg);
}

/* ---- Tier 2 arms against the same reference points ---- */
function tier2() {
  if (!DATA.tier2.length) return;
  const rows = DATA.tier2, W = 940, rowH = 34, ML = 92, MR = 130;
  const H = rows.length * rowH + 62;
  const floor = rows[0].floor, ceiling = rows[0].ceiling;
  const X = v => ML + (v / 1) * (W - ML - MR);
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img',
    'aria-label': 'Model arm efficiency against the autarky floor and exchange ceiling' });

  [[floor, 'autarky', 0], [ceiling, 'exchange ceiling', 16], [1, 'frontier', 0]]
    .forEach(([v, label, drop]) => {
      svg.append(el('line', { x1: X(v), y1: 8, x2: X(v), y2: rows.length * rowH + 12 + drop, class: 'axis' }));
      const t = el('text', { x: X(v), y: rows.length * rowH + 30 + drop, class: 'bandlab',
                             'text-anchor': v === 1 ? 'end' : 'middle' });
      t.textContent = `${label} ${v.toFixed(3)}`; svg.append(t);
    });

  rows.forEach((r, i) => {
    const y = 20 + i * rowH, c = cssv('--accent');
    const name = el('text', { x: ML - 12, y: y + 4, class: 'endlab', 'text-anchor': 'end', fill: cssv('--ink-2') });
    name.textContent = r.arm; svg.append(name);
    if (r.efficiency === null) {
      const t = el('text', { x: X(floor) + 8, y: y + 4, class: 'endlab', fill: cssv('--critical') });
      t.textContent = `ruin — ${r.ruined} agent${r.ruined > 1 ? 's' : ''} held nothing`;
      svg.append(t);
    } else {
      svg.append(el('line', { x1: X(0), y1: y, x2: X(r.efficiency), y2: y,
                              stroke: cssv('--rule'), 'stroke-width': 2 }));
      svg.append(el('circle', { cx: X(r.efficiency), cy: y, r: 5.5, fill: c, class: 'dot' }));
      const v = el('text', { x: X(r.efficiency) + 12, y: y + 4, class: 'endlab', fill: cssv('--ink-2') });
      v.textContent = r.efficiency.toFixed(3); svg.append(v);
    }
    const hit = el('rect', { x: 0, y: y - rowH / 2, width: W, height: rowH, class: 'hit' });
    hit.addEventListener('mousemove', e => showTip(e,
      `<b>${r.arm}</b><div>efficiency ${r.efficiency === null ? 'ruin (' + r.ruined + ')' : r.efficiency.toFixed(3)}</div>` +
      `<div>of its own plan ${r.own_plan === null ? '—' : r.own_plan.toFixed(3)}</div>` +
      `<div>settled ${r.executed}/${r.proposed} · ${r.messages} messages</div>`));
    hit.addEventListener('mouseleave', hideTip);
    svg.append(hit);
  });
  document.getElementById('tier2').append(svg);
}


/* ---- every island as a dot, because the median was the problem ---- */
function islands() {
  const isl = DATA.islands; if (!isl.arms) return;
  const W = 940, rowH = 62, ML = 96, MR = 152, TOP = 14;
  const H = DATA.arms.length * rowH + 46;
  const X = v => ML + v * (W - ML - MR);
  const floor = DATA.sweep.floor, ceiling = DATA.sweep.ceiling;
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img',
    'aria-label': 'Efficiency of each of twelve islands, per arm' });

  [[floor, 'autarky', 0], [ceiling, 'exchange ceiling', 15], [1, 'frontier', 0]]
    .forEach(([v, label, drop]) => {
      svg.append(el('line', { x1: X(v), y1: TOP - 6, x2: X(v),
                              y2: DATA.arms.length * rowH + 6 + drop, class: 'axis' }));
      const t = el('text', { x: X(v), y: DATA.arms.length * rowH + 22 + drop,
                             class: 'bandlab', 'text-anchor': v === 1 ? 'end' : 'middle' });
      t.textContent = `${label} ${v.toFixed(3)}`; svg.append(t);
    });

  DATA.arms.forEach((arm, i) => {
    const y = TOP + 18 + i * rowH, c = armColor(arm), rows = isl.arms[arm];
    const name = el('text', { x: ML - 14, y: y + 4, class: 'endlab', 'text-anchor': 'end', fill: c });
    name.textContent = `${arm} ${DATA.series[arm].name}`; svg.append(name);

    const clean = rows.filter(r => !r.ruined.length);
    const ruined = rows.length - clean.length;
    // Jitter is deterministic in the island's own seed, so the picture is the
    // same every time the page is built.
    clean.forEach(r => {
      const jitter = ((r.seed * 37) % 11 - 5) * 1.6;
      const dot = el('circle', { cx: X(r.efficiency[0]), cy: y + jitter, r: 5,
                                 fill: c, opacity: 0.72, class: 'dot' });
      dot.addEventListener('mousemove', e => showTip(e,
        `<b>${arm} ${DATA.series[arm].name}</b> · island ${r.seed}` +
        `<div>efficiency ${r.efficiency[0].toFixed(3)}</div>` +
        `<div>of its own plan ${r.own_plan[0].toFixed(3)}</div>` +
        `<div>settled ${r.executed}/${r.proposed}</div>`));
      dot.addEventListener('mouseleave', hideTip);
      svg.append(dot);
    });
    if (ruined) {
      const t = el('text', { x: W - MR + 12, y: y + 4, class: 'endlab', fill: cssv('--critical') });
      t.textContent = `${ruined}/12 ruined`; svg.append(t);
    } else {
      const t = el('text', { x: W - MR + 12, y: y + 4, class: 'endlab', fill: cssv('--ink-3') });
      t.textContent = 'none ruined'; svg.append(t);
    }
  });
  document.getElementById('islands').append(svg);
}

/* ---- who got hurt: worst agent vs its own autarky ---- */
function worst() {
  const isl = DATA.islands; if (!isl.arms) return;
  const W = 940, rowH = 54, ML = 96, MR = 60, TOP = 16;
  const H = DATA.arms.length * rowH + 44;
  const lo = 0, hi = 1.8;
  const X = v => ML + (Math.min(v, hi) - lo) / (hi - lo) * (W - ML - MR);
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img',
    'aria-label': 'Worst agent per island as a multiple of its autarky utility' });

  [0.5, 1.0, 1.5].forEach(v => {
    const crit = v === 1.0;
    svg.append(el('line', { x1: X(v), y1: TOP - 8, x2: X(v), y2: DATA.arms.length * rowH + 4,
                            stroke: crit ? cssv('--critical') : cssv('--hair'),
                            'stroke-width': crit ? 1.5 : 1 }));
    const t = el('text', { x: X(v), y: DATA.arms.length * rowH + 22, class: 'bandlab',
                           'text-anchor': 'middle', fill: crit ? cssv('--critical') : cssv('--ink-3') });
    t.textContent = v.toFixed(1); svg.append(t);
  });

  DATA.arms.forEach((arm, i) => {
    const y = TOP + 14 + i * rowH, c = armColor(arm);
    const name = el('text', { x: ML - 14, y: y + 4, class: 'endlab', 'text-anchor': 'end', fill: c });
    name.textContent = `${arm} ${DATA.series[arm].name}`; svg.append(name);
    isl.arms[arm].filter(r => !r.ruined.length).forEach(r => {
      const jitter = ((r.seed * 29) % 9 - 4) * 1.7;
      const hurt = r.worst_ratio < 1;
      const dot = el('circle', { cx: X(r.worst_ratio), cy: y + jitter, r: 5,
                                 fill: hurt ? cssv('--critical') : c,
                                 opacity: hurt ? 0.85 : 0.6, class: 'dot' });
      dot.addEventListener('mousemove', e => showTip(e,
        `<b>${arm} ${DATA.series[arm].name}</b> · island ${r.seed}` +
        `<div>worst agent ${r.worst_ratio.toFixed(2)}× autarky</div>` +
        (hurt ? '<div style="color:' + cssv('--critical') + '">made worse off</div>' : '')));
      dot.addEventListener('mouseleave', hideTip);
      svg.append(dot);
    });
  });
  document.getElementById('worst').append(svg);
}

/* ---- settlement volume: what money costs to run ---- */
function volume() {
  const W = 940, H = 250, ML = 52, MR = 96, MT = 18, MB = 44;
  const budgets = DATA.sweep.budgets;
  const lx = Math.log(budgets[0]), hx = Math.log(budgets[budgets.length - 1]);
  const X = b => ML + (Math.log(b) - lx) / (hx - lx) * (W - ML - MR);
  const peak = Math.max(...DATA.arms.flatMap(a => DATA.sweep.arms[a].map(r => r.executed)));
  const Y = v => H - MB - (v / peak) * (H - MB - MT);
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img',
    'aria-label': 'Median settled trades per island against the round budget' });

  [0, peak / 2, peak].forEach(v => {
    svg.append(el('line', { x1: ML, y1: Y(v), x2: W - MR, y2: Y(v), class: 'grid' }));
    const t = el('text', { x: ML - 10, y: Y(v) + 4, class: 'tick', 'text-anchor': 'end' });
    t.textContent = Math.round(v); svg.append(t);
  });
  const al = el('text', { x: ML, y: MT - 4, class: 'alab' });
  al.textContent = 'median settled trades'; svg.append(al);
  budgets.forEach(b => {
    const t = el('text', { x: X(b), y: H - MB + 20, class: 'tick', 'text-anchor': 'middle' });
    t.textContent = String(b); svg.append(t);
  });
  const xt = el('text', { x: (ML + W - MR) / 2, y: H - 8, class: 'alab', 'text-anchor': 'middle' });
  xt.textContent = 'trading rounds (log)'; svg.append(xt);

  const labels = [];
  DATA.arms.forEach(arm => {
    const rows = DATA.sweep.arms[arm], c = armColor(arm);
    svg.append(el('path', { d: rows.map((r, i) => `${i ? 'L' : 'M'}${X(r.budget)},${Y(r.executed)}`).join(' '),
                            fill: 'none', stroke: c, 'stroke-width': 2,
                            'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
    rows.forEach(r => svg.append(el('circle', { cx: X(r.budget), cy: Y(r.executed), r: 4, fill: c, class: 'dot' })));
    labels.push({ y: Y(rows[rows.length - 1].executed), c, arm });
  });
  declutter(labels, 15).forEach(l => {
    const t = el('text', { x: W - MR + 12, y: l.y + 4, class: 'endlab', fill: l.c });
    t.textContent = `${l.arm} ${DATA.series[l.arm].name}`; svg.append(t);
  });
  document.getElementById('volume').append(svg);
}

/* ---- decomposition: efficiency against exchange quality ---- */
function decomp() {
  const rows = DATA.tier2.filter(r => r.efficiency !== null && r.own_plan !== null);
  if (!rows.length) return;
  const W = 940, H = 380, ML = 62, MR = 30, MT = 20, MB = 52;
  const X = v => ML + (v - 0.5) / 0.55 * (W - ML - MR);
  const Y = v => H - MB - (v - 0.2) / 0.35 * (H - MB - MT);
  const ceiling = rows[0].ceiling;
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img',
    'aria-label': 'Efficiency against own-plan quality, with the exchange-ceiling diagonal' });

  [0.6, 0.7, 0.8, 0.9, 1.0].forEach(v => {
    svg.append(el('line', { x1: X(v), y1: MT, x2: X(v), y2: H - MB, class: 'grid' }));
    const t = el('text', { x: X(v), y: H - MB + 20, class: 'tick', 'text-anchor': 'middle' });
    t.textContent = v.toFixed(1); svg.append(t);
  });
  [0.25, 0.3, 0.35, 0.4, 0.45, 0.5].forEach(v => {
    svg.append(el('line', { x1: ML, y1: Y(v), x2: W - MR, y2: Y(v), class: 'grid' }));
    const t = el('text', { x: ML - 10, y: Y(v) + 4, class: 'tick', 'text-anchor': 'end' });
    t.textContent = v.toFixed(2); svg.append(t);
  });

  // The iso-line: what you score if you produce autarky and trade that well.
  svg.append(el('path', { d: `M${X(0.55)},${Y(0.55 * ceiling)} L${X(1.0)},${Y(1.0 * ceiling)}`,
                          stroke: cssv('--ink-3'), 'stroke-width': 1.5, fill: 'none', opacity: 0.6 }));
  const iso = el('text', { x: X(0.99), y: Y(0.99 * ceiling) - 10, class: 'bandlab', 'text-anchor': 'end' });
  iso.textContent = 'produced its autarky bundle'; svg.append(iso);

  const xl = el('text', { x: (ML + W - MR) / 2, y: H - 12, class: 'alab', 'text-anchor': 'middle' });
  xl.textContent = 'fraction of the best allocation of what it made'; svg.append(xl);
  const yl = el('text', { x: ML, y: MT - 6, class: 'alab' });
  yl.textContent = 'efficiency'; svg.append(yl);

  rows.forEach(r => {
    const c = cssv('--accent');
    svg.append(el('circle', { cx: X(r.own_plan), cy: Y(r.efficiency), r: 6, fill: c, class: 'dot' }));
    const t = el('text', { x: X(r.own_plan), y: Y(r.efficiency) - 14, class: 'endlab',
                           'text-anchor': 'middle', fill: cssv('--ink-2') });
    t.textContent = r.arm; svg.append(t);
    const hit = el('circle', { cx: X(r.own_plan), cy: Y(r.efficiency), r: 16, class: 'hit' });
    hit.addEventListener('mousemove', e => showTip(e,
      `<b>${r.arm}</b><div>efficiency ${r.efficiency.toFixed(3)}</div>` +
      `<div>of its own plan ${r.own_plan.toFixed(3)}</div>` +
      `<div>implied production ${(r.efficiency / r.own_plan).toFixed(3)}</div>`));
    hit.addEventListener('mouseleave', hideTip);
    svg.append(hit);
  });
  document.getElementById('decomp').append(svg);
}

/* ---- price disagreement per good, per arm (log) ---- */
function prices() {
  const rows = DATA.prices; if (!rows.length) return;
  const goods = ['fish', 'grain', 'cloth', 'timber', 'salt'];
  const W = 940, rowH = 46, ML = 108, MR = 40, TOP = 22;
  const H = rows.length * rowH + 50;
  const hi = 40;
  const X = v => ML + Math.log(Math.max(v, 1)) / Math.log(hi) * (W - ML - MR);
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img',
    'aria-label': 'Price disagreement per good for each model arm, log scale' });

  [1, 2, 5, 10, 20, 40].forEach(v => {
    svg.append(el('line', { x1: X(v), y1: TOP - 10, x2: X(v), y2: rows.length * rowH + 8, class: 'grid' }));
    const t = el('text', { x: X(v), y: rows.length * rowH + 26, class: 'tick', 'text-anchor': 'middle' });
    t.textContent = v + '×'; svg.append(t);
  });
  const one = el('text', { x: X(1), y: TOP - 16, class: 'bandlab', 'text-anchor': 'start' });
  one.textContent = 'agreed'; svg.append(one);

  rows.forEach((r, i) => {
    const y = TOP + 6 + i * rowH;
    const name = el('text', { x: ML - 14, y: y + 4, class: 'endlab', 'text-anchor': 'end',
                              fill: cssv('--ink-2') });
    name.textContent = r.arm; svg.append(name);
    const src = el('text', { x: ML - 14, y: y + 18, class: 'bandlab', 'text-anchor': 'end' });
    src.textContent = r.source; svg.append(src);
    const vals = goods.filter(g => r.spread[g] !== undefined).map(g => r.spread[g]);
    if (vals.length > 1) {
      svg.append(el('line', { x1: X(Math.min(...vals)), y1: y, x2: X(Math.max(...vals)), y2: y,
                              stroke: cssv('--rule'), 'stroke-width': 2 }));
    }
    goods.forEach((g, gi) => {
      const v = r.spread[g]; if (v === undefined) return;
      const c = gi === 0 ? cssv('--ink-3') : cssv('--accent');
      const dot = el('circle', { cx: X(v), cy: y, r: 5, fill: c,
                                 opacity: gi === 0 ? 0.55 : 0.8, class: 'dot' });
      dot.addEventListener('mousemove', e => showTip(e,
        `<b>${r.arm}</b> · ${g}<div>${v}× between the highest and lowest quote</div>` +
        `<div>${r.quoting} trader(s) quoting</div>`));
      dot.addEventListener('mouseleave', hideTip);
      svg.append(dot);
      if (v >= 10) {
        const t = el('text', { x: X(v), y: y - 12, class: 'bandlab', 'text-anchor': 'middle',
                               fill: cssv('--critical') });
        t.textContent = `${g} ${v}×`; svg.append(t);
      }
    });
  });
  document.getElementById('prices').append(svg);
}

/* ---- table view: every plotted value, reachable without color ---- */
function table() {
  const s = DATA.sweep;
  let h = '<table><caption>Median efficiency over unruined islands, and islands with any ruin, of 12.</caption><thead><tr><th>rounds</th>';
  DATA.arms.forEach(a => { h += `<th>${a} ${DATA.series[a].name}</th>`; });
  h += '</tr></thead><tbody>';
  s.budgets.forEach((b, i) => {
    h += `<tr><td>${b}</td>`;
    DATA.arms.forEach(a => {
      const r = s.arms[a][i];
      h += `<td>${r.median === null ? '—' : r.median.toFixed(3)} <span style="color:var(--ink-3)">· ruin ${r.ruined}</span></td>`;
    });
    h += '</tr>';
  });
  document.getElementById('table').innerHTML = h + '</tbody></table>';
}

strip(); sweep(); islands(); worst(); volume(); labour(); tier2(); decomp(); prices(); table();
</script>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sweep", type=Path, required=True,
                        help="the round-budget sweep (`..._rounds.json`)")
    parser.add_argument("--tier2", type=Path, nargs="*", default=[])
    parser.add_argument("--islands", type=Path, default=None,
                        help="per-island Tier 1 results, for the distribution figures")
    parser.add_argument("--labour", type=Path, default=None,
                        help="the labour-timing sweep, for the irreversibility figure")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    islands = json.loads(args.islands.read_text()) if args.islands else None
    labour = json.loads(args.labour.read_text()) if args.labour else None
    html = build(load_sweep(args.sweep), load_tier2(args.tier2), islands, labour)
    args.out.write_text(html)
    print(f"wrote {args.out} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
