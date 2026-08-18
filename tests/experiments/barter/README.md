# The island: what has to be shared before a market works

An experiment in what agents must *agree on* — not merely be able to say — for a
market to reach its Pareto frontier, run over a Switchboard hub with a manager
that owns all the state.

```bash
python tests/experiments/barter_experiment.py --islands 12 --rounds-sweep --labour-sweep
python tests/experiments/barter_llm_experiment.py --arms told built  # costs money
python tests/experiments/barter_llm_experiment.py --arms bound --without expiry
pytest tests/test_barter.py tests/test_barter_llm.py -q            # the gates
```

## The island

12 agents, 5 goods (`fish grain cloth timber salt`). Every agent has an
**independent production capacity for every good** — how much it gets for
spending its whole unit of labour on that good — drawn independently, so nobody
is handed a specialty and comparative advantage has to emerge from the draw.
Every agent has Cobb-Douglas tastes, so it wants *some of everything*: a good
you hold none of makes your score zero no matter what else you have.

One unit of labour. That budget is what makes this an economy rather than a free
lunch — without it everyone maxes out every good and there is nothing to trade.

Labour that is offered and not claimed is **recorded and not carried**. A plan
is a split of *this round's* instalment, so fractions summing to less than 1
leave the remainder unworked, and the next round's instalment is the same size
however little of the last one was taken. That is tracked per agent because a
live run finished having spent 0.67 of its labour and nothing in the record
could say whether agents had declined to work or had simply handed in vectors
that summed short — opposite findings that looked identical.

**When** it is spent is a knob, not a constant. At one instalment it is a single
irreversible bet placed before any price exists; sliced across the trading rounds
it is the same unit spent a little at a time, against what the market has
actually delivered. The frontier, the autarky floor and the exchange ceiling are
identical either way, so the two are directly comparable and the only thing
varying is whether a commitment can be revised. That turns out to matter more
than anything on the convention ladder — see *Irreversibility* below.

## The round

Every round is the same four stages, each with a deadline the **manager**
enforces rather than the agents observing:

| | stage | what is accepted |
|---|---|---|
| 1 | **discovery** | talk. Neither labour nor trades. |
| 2 | **production** | this round's instalment of labour, and nothing else. |
| 3 | **deal** | talk again. Still no trades. |
| 4 | **offer** | proposals. Each escrows its side as it is made. |
| 5 | **resolve** | talk again. Nothing proposed, nothing approved. |
| 6 | **settle** | approvals and withdrawals. |

`discovery -> production -> [deal] -> trading -> [resolve] -> trading ->
discovery ...`, and each transition has exactly one legal predecessor, so a
stage cannot be re-entered or taken out of order. `deal` and `resolve` are
skippable by a run that has no such stage.

**Three talking stages per round**, because they are three different
conversations. In stage 1 an agent is guessing what it will hold and can still
change *what it makes*; by stage 3 it knows, and so does everyone else, so terms
can be agreed against real inventory instead of intent; by stage 5 it has escrow
on the table and a specific collision to settle with a specific counterparty.
The original shape put all the talking up front with the whole production
decision behind it, which made every later word a negotiation about goods nobody
could change.

**Stage 5 exists because of what stage 4 creates.** Offers escrow as they are
made, so two agents who agreed a swap and both proposed it now hold mirror-image
trades and one has to give way. Every route out — approve one and cancel the
other, approve both and swap twice, cancel both and start again — needs the two
of them to choose the *same* one, and **none is right on its own merits**. It is
a pure tie-break, which makes it the smallest instance of the thing this whole
experiment is about: the failure mode is not choosing badly but choosing
differently. Two traders who both defer cancel both offers; two who both insist
swap twice.

**Scripted agents are handed a rule. Model agents are not.** Tier 1's traders
apply *first proposed survives*, read off the trade ids, so both sides reach the
same answer without saying anything — that is what makes them a benchmark rather
than a coin flip, and it is not a claim that the rule is clever. Newest-survives
would do exactly as well. A model island gets the stage and no rule, and whether
it invents one *its counterparty also arrives at* is the measurement. Two
switches separate the halves of that: `crossings` names the collision,
`tiebreak` states a rule, and both default off.

**Offering and answering stay separate passes.** With one turn per agent,
roughly half of all offers cannot be answered until the following round, and a
three-tick expiry gives a proposal one or two real chances at being seen.

Two things fall out of this structure rather than being fixed by it. Rolling
labour is native — one instalment per round, in a stage that exists for it —
and the tick collision that used to eat the whole first trading round's
instalment cannot arise, because production and trading are separate stages with
the clock moving between rounds. That bug cost one live run **14% of its total
labour**, and it read in the results as agents declining to work.

Agents can read the whole public floor at any time via `history`, which is a
**tool call rather than something pasted into every turn note**. Each agent holds
one session for the entire island, so its own past is already in context and
costs nothing to keep; re-sending the shared floor every turn would grow with the
square of the run. Fetching on demand keeps the cached prefix append-only, which
is what makes a long island affordable.

## Offers collide, and nothing resolves that for you

An agent may make as many offers as it likes in the trading stage, and **each
escrows its side the moment it is proposed**. So two agents who agree a swap in
the deal stage and both go on to propose it end up with two live trades, twice
the goods locked up, and a decision neither planned for: approve one and cancel
the other, or approve both and swap twice.

The manager names the collision and does nothing about it — no matching, no
cancelling the second, no refusing the proposal. It is the only thing that can
move a quantity, so a manager that quietly resolved collisions would be doing
the convention's job and the run would be measuring the manager instead.

Whether agents are *told* about a collision is a switch (`crossings`, off by
default). Both halves are in the reply either way — one under
`your_open_offers`, one under `awaiting_your_approval` — so the switch controls
the naming, not the facts. Noticing unaided is the interesting outcome.

They are common, and the arms differ sharply in how many they generate:

```
arm        crossings   both   one  neither
A silent          64      1    20       43
B disclose       116      0     5       111
C price           18      0     9         9
D money          128      5    36        87
```

**A shared price produces six times fewer.** Arm C picks the counterparty with
the mirror position and offers at the agreed rate, so it is aiming rather than
casting about; money generates the most because it alternates buying and selling
against whoever has cash. `both` — the pair swapping twice because neither side
backed out — stays rare.

One property worth knowing, because it is not obvious and was not what I
expected. When a crossing is over the same goods, each side may have escrowed
the very thing the other's offer asks for, so the first approval fails. That
looks like a deadlock and is not: **a failed approval returns the offer**,
releasing that escrow, which leaves the other side able to settle. A crossing
costs at most one of the two trades. The alternative — leaving a doomed offer
sitting on its escrow until it times out — would turn every mis-sized crossing
into a stall of several rounds.

## The manager

One state machine, reached over Switchboard messages, is the only thing that can
change a quantity. It is **not a model**, deliberately: it enforces invariants,
and an invariant you can argue your way out of is not one.

- **Non-negativity**, checked at every mutation.
- **Conservation** — holdings plus escrow equals everything ever produced.
- **Two-phase settlement** — the buyer proposes and gets a trade id back; the
  named seller approves that id; nobody else can, and it settles once.

A proposal **escrows the buyer's side immediately**. Without that a buyer can
promise the same ten fish to five sellers, every quote becomes a lottery ticket,
and you cannot study conventions in a market where a quote is not binding.
Proposals expire on their own and release their escrow — the lease argument
applied to goods.

It is an *application on the four primitives*, not a fifth one: blackboard for
state, messages for request/reply, a lease as the state mutex, a channel for the
public ledger. `test_hub_and_direct_transports_agree` asserts that running the
whole market over a real hub gives bit-identical results to calling the state
machine in process — the hub carries the market without changing it.

## Scoring: a bracket, not an estimate

An outcome is scored by **distance to the Pareto frontier**: 1.0 means no
reallocation could make everyone better off; 0.5 means everyone could have had
twice as much of everything. A single welfare number would be gameable — maximise
the sum and an arm wins by immiserating somebody — so the frontier is the whole
surface and *where on it* an arm lands is reported separately.

`efficiency` returns a **certified interval**, proved from two directions that
share no code: an allocation that shows a level is reachable, and a price vector
that shows nothing better is. A run that failed to converge shows up as a wide
bracket instead of a confident wrong number. The self-test is the First Welfare
Theorem — the competitive equilibrium must score 1.000, and it does.

Three benchmarks make the numbers readable:

| | |
|---|---|
| **autarky floor** ≈ 0.40 | nobody trades |
| **exchange ceiling** ≈ 0.48 | trade *perfectly*, but keep making what you would have made alone |
| **frontier** 1.000 | |

That gap is the first finding, and it decides what agents ought to be talking
about: **swapping perfectly recovers about a seventh of what is on the table.**
The rest requires having produced different things. Trading skill is not the
binding constraint; knowing what to make is.

## Tier 1 arms (scripted)

An information ladder, removing one thing at a time. The Tier 2 arms further
down are named rather than lettered, and are a different ladder.

| | |
|---|---|
| **A silent** | agents can call the manager and nothing else |
| **B disclose** | a public channel; everyone posts their marginal values |
| **C price** | same channel, *same information*, plus an agreed way to read it into one public price |
| **D money** | arm C plus one clause: settle in the numeraire, and accept it past your own appetite |

**B vs C holds information constant and varies only the protocol.** **C vs D
holds the protocol constant and varies only what the numeraire is for.**

## Results (12 islands, scripted policies)

```
 ROUNDS          A silent        B disclose           C price           D money
     15   0.466 ruin 0/12   0.401 ruin 0/12  0.952 ruin 10/12    -   ruin 12/12
     30   0.475 ruin 0/12   0.444 ruin 0/12   0.964 ruin 8/12    -   ruin 12/12
     60   0.476 ruin 0/12   0.457 ruin 0/12   0.997 ruin 6/12  0.872 ruin 10/12
    120   0.477 ruin 0/12   0.457 ruin 0/12   0.998 ruin 6/12   0.978 ruin 4/12
    240   0.478 ruin 0/12   0.458 ruin 0/12   0.999 ruin 6/12   0.980 ruin 4/12
```

*Ruin* means an agent finished holding none of some good — zero utility. It is
counted, never averaged in, because a mean that swallows a zero hides the only
outcome anyone would care about.

**Blind bilateral trading is already an excellent swapper and that is not the
problem.** Arm A reaches 0.97 of the best possible allocation *of what it chose
to produce*, and still scores 0.47 overall, because it produces its autarky
bundle and nothing tells it to do otherwise. It is also perfectly safe: nobody
is ever ruined, nobody ever finishes below autarky.

**Talking, without an agreed way to read what is said, is worse than silence.**
Arm B publishes true information and lands *below* arm A. Every agent averages
the floor its own way, so each specialises against a slightly different price,
and then they cannot trade with each other — 14 settled trades against arm A's
120. Communication is not free: it induces commitment without producing
coordination.

**A shared price is what converts disclosure into specialisation that pays.**
Arm C reaches the frontier *exactly* — 1.000, the competitive equilibrium,
found by agents applying a common update rule to public posts with no
auctioneer and no help from the manager.

**And then it ruins half its islands, permanently.** This is the result worth
the whole experiment. Arm C's ruin rate stalls at 6/12 and **never improves
however long it runs** — 60 rounds or 240, it is stuck. A shared price
tells two agents what a fair swap is; it does not make the agent holding the
only pile of fish *want* your cloth. That is the double coincidence of wants, and
no amount of agreeing on prices dissolves it. Meanwhile specialisation has turned
every agent into a one-good holder, so when settlement fails the loss is total
rather than marginal.

**The clause that fixes it is not about prices at all.** Arm D changes one
thing: the numeraire is accepted *past the point of wanting it*, because it can
be spent again. Its ruin rate falls with time where C's does not — 12, 12, 10,
4, 4 — and its efficiency climbs to 0.980. The two failures look identical at any single round
budget and are completely different: **C's is structural and D's is merely
slow.** That distinction is only visible because the round budget is swept
rather than chosen, which is also why quoting any single number for these arms
would have decided the result by picking the budget.

Money buys robustness and charges for it in transaction volume: arm D settles
roughly twice as many trades as arm C to get there.

## Who the gains went to

Efficiency is **distribution-neutral by construction** — it scales every agent
by the same factor, so it measures how much was wasted and says nothing about
who got it. That is a virtue, and it means the other question needs its own
column. Every number below is an agent's realised utility as a multiple of
**its own** autarky utility, because Cobb-Douglas utilities are not
interpersonally comparable: each is defined only up to its own monotone
transformation, so a Gini or a Nash product over raw utilities would be
arithmetic without meaning, while a ratio to an agent's own counterfactual is a
true statement about that agent.

Voluntary trade cannot put anybody below 1.0 — every settled trade passed both
sides' accept test — so a seat under its own autarky is always a **production
bet placed on a price that did not materialise**.

```
             seats  ruined (0.00x)  harmed (0<r<1)   gained   worst partial loss
silent         144          0 (0%)          0 (0%)     100%   —
disclose       144          0 (0%)        67 (47%)      53%   0.50x
price          144        17 (12%)          1 (1%)      88%   0.67x
money          144        18 (12%)         11 (8%)      80%   0.01x
```

**The arms differ in the *shape* of their harm, not merely its amount**, and
the shapes are qualitatively different in a way no single number could show:

* **silent** never harms anybody. Every one of 144 seats gains.
* **disclose** ruins nobody and yet makes **47% of agents worse off than not
  taking part**, none of them catastrophically. Broad and shallow — the damage
  of specialising against a price nobody else held.
* **price** is almost perfectly bimodal: 13% wiped out, 86% gaining, and
  **exactly one seat in between**, at 0.99x. Under a shared price you are ruined
  or you gain; there is essentially no middle. That is what specialisation on a
  correct price does — it works or it strands you.
* **money** carries a tail of *severe* partial losses, down to **0.01x** — an
  agent left with a hundredth of what it would have had by never trading. It
  buys its robustness over time and pays in dispersion.

Note that `worst` cannot tell any of this apart. `disclose` and `money` both
report a worst agent around 0.5x and 0.3x; one of them harmed 63 agents and the
other 22, and one of those groups was wiped out while the other was merely
disappointed.

### The tie-break is not free

Every number above is with the scripted tie-break in place. It does what it was
built for — **no pair anywhere swaps twice, in any arm** — but it is not a
free improvement, and the two arms it matters to move in opposite directions:

| at 60 rounds | ruin before | ruin after | trades before | trades after |
|---|---|---|---|---|
| **C price** | 8/12 | **6/12** | 91 | **115** |
| **D money** | 7/12 | **10/12** | 200 | **170** |

The reason is that the two arms have different bottlenecks. **Price is limited by
matching**, so withdrawing the duplicate half of a crossing removes an offer that
was never going to settle anyway and frees the escrow behind it — more trades,
less ruin. **Money is limited by throughput**: its whole mechanism is the
numeraire circulating, and half of every crossing is a lap that no longer
happens. It recovers with time (4/12 by 240 rounds) but it is worse at 60 than
it was without the rule.

So a shared convention that removes redundant commitments helps a market whose
problem is finding the counterparty and hurts one whose problem is volume. That
is a result about conventions rather than about this rule, and it is the sort of
thing that only shows up because both arms run the same tie-break.

## Irreversibility: the ruin was never an information problem

Every rung of the ladder above is trying to make one irreversible bet a *better*
bet. The bet is production: labour is committed before any trade has happened,
and nothing afterwards can unwind it. **Slicing the same unit of labour across
the trading rounds attacks the loss from the other side** — it lets a wrong bet
be revised. Nothing else changes: no extra messages are sent, no extra prices
are formed, and the frontier and both benchmarks stay exactly where they were.

```
INSTALS          A silent        B disclose           C price           D money
      1   0.476 ruin 0/12   0.457 ruin 0/12   0.997 ruin 6/12  0.872 ruin 10/12
      2   0.341 ruin 0/12   0.443 ruin 0/12   0.683 ruin 9/12   0.656 ruin 7/12
      4   0.510 ruin 0/12   0.436 ruin 0/12   0.620 ruin 7/12   0.590 ruin 7/12
     16   0.578 ruin 0/12   0.429 ruin 0/12   0.528 ruin 0/12   0.484 ruin 1/12
     61   0.671 ruin 0/12   0.399 ruin 0/12   0.496 ruin 0/12   0.469 ruin 0/12
```

**Arm C's ruin — flat at 6/12 however long it ran, the result the whole
experiment turned on — goes to zero.** So does arm D's. Neither needed anything
said to anybody. Arm D's clause exists precisely to dissolve the double
coincidence of wants, and it needs two hundred and forty rounds to get ruin down
to 4/12; sixteen instalments of labour take both arms to 0 or 1 in sixty.

**And it is a scissors, not a free win.** Efficiency on the islands that survive
falls by about as much as ruin does, because an agent that keeps re-aiming at
what it is short of stops making what it is best at. Both halves are printed,
plus a third view that weighs them against each other by scoring a ruined island
at the zero it literally is:

```
INSTALS          A silent        B disclose           C price           D money
      1             0.476             0.457             0.000             0.000
      4             0.510             0.436             0.000             0.000
     16             0.578             0.429             0.528             0.478
     61             0.671             0.399             0.496             0.469
```

Read the zeros carefully: **under a shared price the median island is a ruined
island** until labour can be revised. That is what arm C's celebrated 0.999
was hiding — it is a median over the four islands the arm did not wreck.

Two things this table does not say, and one it says by accident:

* **It is not "more slicing is always better".** Arm A drops from 0.476 to 0.341
  at *two* instalments before climbing to 0.671 at sixty-one. A coarse instalment
  is a worse bet than the one-shot spread, because it commits half the labour to
  a single good; only fine slicing smooths back out.
* **Arm A is partly a policy improvement, not only a timing one.** Its rolling
  rule — make whatever raises utility fastest per unit of labour — is simply
  better than the alpha split once trade exists. The comparison is honest about
  the world (a rolling world admits better policies) and it is not purely about
  timing for that arm.
* **By the third view, silence wins everywhere.** Once production can be revised,
  no rung of the convention ladder beats an agent that says nothing and produces
  greedily against what it is short of. The conventions bought specialisation,
  and specialisation is what irreversibility made dangerous.

The second and third rows of the printed table are guardrails rather than
results. The median over unruined islands is taken over a subset that this sweep
is itself moving, so a number can improve because the hard cases dropped out.
The "same islands at every setting" view fixes that and, for arm C, is empty —
which is itself the finding, stated honestly rather than papered over.

The choice of rolling policy does not drive any of this. An alternative rule that
keeps specialising and only re-ranks away from goods the market would not take
gives 0.504 against 0.508 at sixty-one instalments, and identical medians on the
common subset.

Nor does the tie-break, though it is not neutral either — see below.

## What Tier 1 cannot tell you

These policies are written, so this tier **cannot discover that communication
helps** — it was put there. What it establishes is everything that makes a model
run interpretable: the size of the prize, a scorer that agrees with theory, proof
that the two-phase escrow protocol can reach the frontier (so anything short of
it is about the agents, not the apparatus), and the knowledge that the gains live
in production rather than in swapping.

## Tier 2: models in the trader's seat

`barter_llm_experiment.py` puts a language model in each agent, with the manager
unchanged and still not a model. Four arms:

| | |
|---|---|
| `silent` | no channel tool at all |
| `free` | `say` and `listen`, and nothing about what to put in them |
| `told` | the numeraire convention, **stated in words**. Same tools as `free` |
| `built` | the same words, **plus machinery**: a structured quote board with validation and a median |

`silent` and `free` never hear the words "price", "numeraire" or "money" — so a
convention appearing in `free` was invented, not followed, and the Tier 1 ladder
is what gives it a scale.

### The arms are combinations, not primitives

Every one of those names is a bundle, and that was a flaw. `built` added storage
*and* aggregation in one step; `bound` added a deviation report *and* quote
expiry. When a rung moved, the run could say the bundle mattered and could never
say which half of it did — so every result was an attribution to a name rather
than to a mechanism.

So **everything told to an agent is now an independent switch**, and the named
arms are combinations of them:

| switch | what it hands over | where it lands |
|---|---|---|
| `channel` | `say` and `listen` | tools + one paragraph |
| `numeraire` | fish is the unit of account | words |
| `board` | `post_quote` validates, `read_quotes` returns everyone's latest | tools |
| `median` | the board also reports the median per good | tools |
| `deviation` | the board reports *your* distance from the median | tools |
| `expiry` | quotes go stale unless renewed | tools |
| `money` | accept the numeraire past wanting it | words |
| `pay_tool` | `pay` sizes a money trade at the median and proposes it | tools |
| `rolling` | labour is committed in instalments | the world, and the words that describe it |
| `ruin_warning` | spell out that a zero holding scores zero | words |
| `horizon` | how many rounds remain | turn note |
| `labour_left` | how much labour you still have | turn note |
| `own_value` | `my_state` works out what one more of each good is worth to you | tool reply |
| `own_score` | `my_state` reports your live score | tool reply |

```
silent  →  (nothing)
free    →  channel
told    →  channel numeraire
built   →  channel numeraire board median
bound   →  … deviation expiry
spend   →  … money
paid    →  … pay_tool
```

Any of them can be flipped alone:

```bash
python tests/experiments/barter_llm_experiment.py --arms bound --without expiry
python tests/experiments/barter_llm_experiment.py --arms built --with expiry
```

Neither of those has a name on the ladder, because neither is a rung — they are
the differences *between* rungs, which is what an attribution needs and what a
ladder of bundles cannot give. Switching one off takes its dependents with it (a
board with no numeraire would be quoting on a scale nobody was told about), and
the run record stores the **resolved** switch set, so an island always reports
what it actually had rather than what was asked for.

Three properties are gated rather than intended. Every preset is pinned switch by
switch, so the six paid islands already banked still describe setups that exist.
Turning an *affordance* on or off never moves a word of the system prompt — the
claim the whole ladder rests on, now asserted over every tool switch instead of
one pair of arms. And a rolling island is never told it spends its labour "once,
at the start", which it was, for one paid run, while the manager underneath was
accepting instalments.

**`told` against `built` is the pair that matters, and they share a system prompt
byte for byte** — a test asserts it. The only difference is whether the
convention has an affordance or is only described. That is the difference between
telling agents how to coordinate and building them something to coordinate
*with*, and it is a question about what a coordination substrate should offer
rather than about what a prompt should say. If they match, the machinery is
ceremony. If `built` wins, knowing a convention and being able to run one are
different things.

The machinery is deliberately modest, and each piece earns its place:

* `post_quote` **validates** — a number per good or an explanation. Under `told`
  a trader can say "cloth is about two, maybe three" and the ambiguity survives
  all the way to the trade.
* fish is pinned at 1, so two traders cannot quote on different scales while
  appearing to agree.
* `read_quotes` returns the **median** per good. Turning scattered quotes into
  one number everybody computes identically is the step a price convention
  actually needs, and it is the step `told` leaves each agent to do in its head,
  from prose — which is where a shared price stops being shared.

The manager still knows nothing about prices and will settle any trade both sides
agree to, at any rate at all. A convention it policed would be a rule instead,
and there is a test for that too.

Read a Tier 2 result against the ladder: near the exchange ceiling means the
models traded but never coordinated production; well above it means they found
something, and the kept transcript says what. The content of what agents invent
is the finding — no aggregate can carry it.

The short version of what came back: **models adopt a convention's vocabulary
instantly and its substance not at all**. Read the arm table with its caveat
attached, though — single-island variance turned out to swamp the differences
between arms, so what Tier 2 has produced is a catalogue of failure modes rather
than a ranking of designs.

### One island each arm, Haiku 4.5, 4 agents, 5 rounds

#### `silent` vs `free` — is the convention something models invent?

```
                          silent       free         benchmarks
  EFFICIENCY                 0.337        0.386     autarky floor    0.374
  of its own plan            0.836        0.949     exchange ceiling 0.413
  worst agent vs autarky     0.63x        0.93x     scripted price   1.000
  settled / proposed          9/36         5/30
  messages                       0           14
```

**Silent models land below not trading at all** (0.337 against a 0.374 floor),
with one agent finishing at 0.63x what it would have had by ignoring everybody.
They are not bad at trading — 0.836 of the best allocation of what they chose to
make. They moved off the autarky production plan, which is the best plan
available to someone who cannot coordinate, and could not trade their way back.
They chose wrong, silently. That is the same shape as scripted arm B, arrived at
independently.

**Talking helps, and helps most by preventing harm.** Arm B clears the autarky
floor, and the worst-off agent goes from 0.63x to 0.93x. Most of the gain is in
not wrecking anybody rather than in reaching anything new: 0.386 against an
exchange ceiling of 0.413 is a market that swaps its existing goods competently
(0.949 of its own plan) and still never coordinated what to produce.

**And here is what they said.** Every one of the fourteen messages is an
advertisement of a want or a holding. Twelve of fourteen name something the
sender needs; eight name something it has. **Zero mention a price, a rate, an
exchange ratio, a numeraire, or any unit of account at all** — checked by
grepping the transcript for terms language, not by impression.

```
a2: Hi all! I'm a2. ... Most interested in getting more salt and grain.
a4: Critical need: I'm looking for cloth! I have excess grain and fish to trade.
a1: I'm critically low on fish (0.0099) and desperately need more. I have
    abundant salt (0.5433 + 0.25 escrow) and solid grain/cloth holdings.
a3: I cannot approve t5 and t10 due to insufficient holdings...
```

They understood the game — "I need fish to avoid a zero score" is an agent
correctly reading its own Cobb-Douglas exponent — and they used a free public
channel to build a **want-board rather than a market**. They coordinated on
identity and need, and never once on *terms*. That is precisely the scripted
"disclose" rung, and they did not climb to the "price" rung above it, where the
frontier actually is.

The settlement rate says the same thing from the other side: 25 of 30 proposals
never settled. Without agreed terms an agent cannot tell whether an offer is fair
before committing, and cannot tell what a counterparty can actually cover —
"I cannot approve t5 and t10 due to insufficient holdings."

#### `told` vs `built` — instruction, then machinery

```
arm            eff   own plan   worst   settled   msgs   quotes
free         0.386      0.949    0.93      5/30     14        0
told         0.316      0.771    0.77      2/25     15        0
built        0.368      0.898    0.93      3/16     11        6

autarky floor 0.374     exchange ceiling 0.413     frontier 1.000
```

**Telling models the convention made them worse than telling them nothing.**
`told` lands at 0.316 — below `free`, and below the autarky floor. Settlement
nearly collapsed, 2 trades of 25 proposed.

They adopted the vocabulary instantly and completely. Every `told` agent quoted
prices in fish, exactly as asked:

```
a2: a2 prices: grain 1.01, cloth 0.21, timber 1.11, salt 1.47 per fish
a4: a4 prices: grain 0.5,  cloth 1.8,  timber 1.2,  salt 0.5  per fish
```

Recovering each agent's final price vector from the transcript — the work a
counterparty actually has to do — gives cloth quoted **30x apart**, salt 7.9x,
grain 3.1x, and only three of four agents posting a parseable price list at all.
One agent opened by copying another's numbers verbatim to two decimals across
four goods, then later posted a completely different vector (cloth 0.21 → 1.02,
salt 1.47 → 0.08). No anchoring, no convergence.

Meanwhile every message describes its offers as "fair rates" and "fair value
trades". That is the mechanism of the harm, and it is the Tier 1 result one level
up: **the words of a convention are not the agreement.** Agents who merely talk
know they disagree; agents handed a shared vocabulary act on prices they *believe*
are shared while holding them 30x apart.

**The machinery repairs most of that self-harm and does not deliver the
convention.** `built` recovers 0.316 → 0.368, the worst-off agent 0.77x → 0.93x,
and its allocation of its own production 0.771 → 0.898. Proposals get better
targeted — 16 made instead of 25, and *none* rejected instead of four. But it
still does not beat `free`, and the final quote board explains why:

```
a1: fish 1   grain 3.5  cloth 15  timber 0.5  salt 1
a2: fish 1   grain 2.5  cloth 50  timber 1.1  salt 2.2
a3: fish 1   grain 3.5  cloth 65  timber 1.5  salt 3
a4: fish 1   grain 0.6  cloth 2.4  timber 1.4  salt 1.5
```

Cloth spans **27x**, grain 5.8x, salt and timber 3x. The one good every trader
agrees on is `fish` — **the good the machinery pins for them.** Everything left
to the traders stayed 3x to 27x apart. Validation and a fixed scale removed the
ambiguity that a validator can remove; the `median_price` sitting in every
`read_quotes` reply was available to all four and moved nobody. They posted
quotes and read quotes, and never revised toward each other.

That is the difference between the scripted arm C and every model arm. Scripted
C converges because its policy *revises* every round against aggregate excess
demand. The models had the aggregate and used it as a display. **Posting a price
and agreeing a price are different things, and machinery that aggregates without
obliging revision only delivers the first.**

Underneath all three arms is a quieter failure that matters more. Every model arm
sits at or below the exchange ceiling of 0.413, which means **none of them ever
specialised production** — they used prices, when they had them, to haggle over
stock they already held. But Tier 1 says the exchange gains are about a seventh
of what is on the table. The convention's whole economic value is in telling you
*what to make*, and no arm got near that, machinery or not.

#### `bound` — the board pushes back

```
arm          eff  own plan  worst   settled  msgs  quotes    agree
free       0.386     0.949   0.93      5/30    14       0        -
told       0.316     0.771   0.77      2/25    15       0    30.0x
built      0.368     0.898   0.93      3/16    11       6    27.1x
bound   ruined 1     0.000   0.00      1/21    17      11     1.7x
```

**Obligation was the missing ingredient, and it worked.** Reporting each agent's
own deviation and expiring stale quotes took price disagreement from 27.1x to
**1.7x** — cloth from 27.1x to 1.5x, grain 5.8x to 1.5x, salt 3.0x to 1.2x. The
final board is four traders who genuinely agree:

```
a1: fish 1  grain 1.5   cloth 2.25  timber 3.0  salt 1.8
a2: fish 1  grain 1.4   cloth 2.0   timber 2.2  salt 1.7
a3: fish 1  grain 1.75  cloth 2.25  timber 2.1  salt 1.8
a4: fish 1  grain 1.2   cloth 3.0   timber 1.8  salt 2.0
```

Nothing in the prompt changed. The same words that produced 30x disagreement
produced this once the board named your distance from the median and stopped
carrying quotes you had abandoned. **What the convention needed from its
substrate was pressure, not information.**

**And it produced the worst outcome of any arm.** One agent ruined, one trade
settled of twenty-one proposed, nine offers expiring unanswered. Final utilities
`[0.319, 0.192, 0.353, 0.0]`.

The transcript of the agent that went to zero is worth reading in full, because
it is not confusion:

```
a4: URGENT: I need salt to avoid a zero score. a1, my t6 offer (0.6 grain
    for 0.8 salt) is fair at your prices—please approve!
a4: DESPERATE ROUND 4: I have ZERO salt - my score is 0. I need ANY salt
    immediately. I can trade: 0.18 timber, 0.06 cloth, fish/grain...
a4: Final round! I have zero salt which makes my score zero. I'm counting on
    a1 and a3 to approve trades t17 and t18—these are fair at your quoted
    prices and critical for me to get any score at all. Please approve!
```

t17 and t18 **expired unapproved. Nothing was rejected all run.** a4 knew what
it needed, knew what everything was worth, offered fair value at prices every
counterparty had publicly agreed to — and could not buy salt, because a1 and a3
simply did not want what a4 had.

That is the **double coincidence of wants**, and it is exactly where scripted arm
C died: shared price reached, frontier reachable, and a third of islands ruined
at a rate that never improved however long they ran. Two completely different
routes — hand-written tatonnement policies, and Haiku agents choosing freely —
hit the same wall the moment they achieved a shared price. That agreement across
tiers is worth more than either result alone, because nothing was shared between
them but the economy.

Tier 1 already knows the answer, and no model arm has it. Arm D's one clause —
*accept the numeraire past the point of wanting it, because you can spend it
again* — took scripted ruin from 12 islands to 3. A unit of account tells two
agents what a fair swap is. It does not make anyone want your cloth. The model
ladder has now climbed to precisely the rung where money becomes necessary, and
stopped there.

#### `spend` and `paid` — money, and a calculator for it

```
arm          eff  own plan  worst   settled  msgs  quotes    agree
free       0.386     0.949   0.93      5/30    14       0        -
told       0.316     0.771   0.77      2/25    15       0    30.0x
built      0.368     0.898   0.93      3/16    11       6    27.1x
bound   ruined 1     0.000   0.00      1/21    17      11     1.7x
spend      0.272     0.645   0.36      9/30    19      10     6.7x
paid    ruined 2     0.000   0.00      0/26    25       9    14.0x
```

`spend` adds the money clause in words to `bound`'s board; `paid` adds the `pay`
calculator on top. **`spend` did what money is for**: nine trades settled against
`bound`'s one, and nobody ruined. The double coincidence dissolved, exactly as
Tier 1's arm D says it should.

**And `paid` settled nothing at all.** Its transcript shows why, and the reason
is not economic:

```
a1: I can't approve t14 and t15 because I don't currently have grain or cloth
    to give. But I need your approvals to get grain (t13 to a4) and timber
    (t12 to a3) first.
```

That is a **circular wait**. Every agent's ability to settle depends on somebody
else settling first, and escrow makes it bite harder — goods committed to a
pending offer are unavailable for the offer that would unblock it. It is a
distributed-systems deadlock wearing an economy's clothes, and it is the kind of
finding a transcript can carry that no aggregate can.

#### What this is and is not — read this before the table above

**The arm-to-arm ordering in that table is not supported by this data, and I
should not have implied otherwise for `bound`.**

`bound`, `spend` and `paid` all run the same board machinery on the same island
and seed, differing by two sentences and one calculator. They produced:

| | `bound` | `spend` | `paid` |
|---|---|---|---|
| settled trades | 1/21 | 9/30 | 0/26 |
| price agreement | 1.7x | 6.7x | 14.0x |
| agents ruined | 1 | 0 | 2 |

Those are not small differences between near-identical setups — they are the
same size as every effect this ladder has claimed. **The honest reading is that
single-island run-to-run variance swamps the arm differences.**

That specifically retracts something said earlier here: that `bound`'s 27.1x →
1.7x price convergence was too large to be a draw. The same board went on to
produce 6.7x and 14.0x. The board may well help — Tier 1 says a shared price is
worth a great deal — but **one island cannot show it, and this file previously
said it could.**

What does survive:

* **Tier 1.** Twelve islands, every arm swept across five round budgets. Those
  numbers are replicated and the structural claim in them — C's ruin flat at
  8/12 while D's falls 12→3 — rests on a sweep, not a draw.
* **Mechanisms visible in transcripts.** An agent explaining that it starved
  holding fair offers nobody wanted, or that it cannot approve until someone
  approves it first, is legible regardless of how the run scored. These are
  observations about *what can go wrong*, not estimates of how often.

What is needed next is **replication, not a seventh arm**: the same arm over
several seeds, to measure within-arm variance directly. Until that exists, every
Tier 2 number here is an anecdote, and the ladder is a catalogue of failure modes
rather than a ranking of designs.

Limits, restated: every agent-turn is a model call, so one arm of one island
costs $2–5 and the better part of an hour; agents take turns rather than acting
concurrently, and concurrency is where coordination is hardest; it is one model
(Haiku 4.5) and the behaviour may be model-specific. Raw records for all six arms
are alongside this file as `tier2_seed1_*.json`.

## Files

| | |
|---|---|
| `economy.py` | the island, the frontier, the certified efficiency bracket. No Switchboard, no agents — if the scorer imported the manager, a bookkeeping bug could move the frontier to meet the allocation |
| `manager.py` | the state machine and its Switchboard service |
| `traders.py` | the four scripted policies |
| `run.py` | one island end to end, over either transport |
| `flow.py` | the order of play, with nothing in it that knows about models |
| `llm.py` | the model-facing tool surface, and `Telling` — every switch there is |
| `analysis.py` | reading prices back out of prose or off the board, and the trajectory |
| `report.py` | the findings page, built from the run records so the charts cannot drift |
| `tier1.json` `tier1_rounds.json` `tier1_labour.json` | the scripted results the page is drawn from, all three written by one command |
| `tier2_seed1_*.json` | the raw record of each paid island |
