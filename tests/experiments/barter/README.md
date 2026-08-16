# The island: what has to be shared before a market works

An experiment in what agents must *agree on* — not merely be able to say — for a
market to reach its Pareto frontier, run over a Switchboard hub with a manager
that owns all the state.

```bash
python tests/experiments/barter_experiment.py --islands 12 --rounds-sweep
python tests/experiments/barter_llm_experiment.py --arms told built  # costs money
pytest tests/test_barter.py tests/test_barter_llm.py -q            # the gates
```

## The island

12 agents, 5 goods (`fish grain cloth timber salt`). Every agent has an
**independent production capacity for every good** — how much it gets for
spending its whole unit of labour on that good — drawn independently, so nobody
is handed a specialty and comparative advantage has to emerge from the draw.
Every agent has Cobb-Douglas tastes, so it wants *some of everything*: a good
you hold none of makes your score zero no matter what else you have.

One unit of labour, spent once. That budget is what makes this an economy rather
than a free lunch — without it everyone maxes out every good and there is
nothing to trade.

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
     15   0.468 ruin 0/12   0.401 ruin 0/12  0.994 ruin 11/12    -   ruin 12/12
     30   0.474 ruin 0/12   0.435 ruin 0/12  0.992 ruin 10/12  0.770 ruin 11/12
     60   0.474 ruin 0/12   0.455 ruin 0/12   0.999 ruin 8/12   0.831 ruin 7/12
    120   0.475 ruin 0/12   0.465 ruin 0/12   1.000 ruin 8/12   0.972 ruin 5/12
    240   0.476 ruin 0/12   0.466 ruin 0/12   1.000 ruin 8/12   0.976 ruin 3/12
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
and then they cannot trade with each other — 18 settled trades against arm A's
128. Communication is not free: it induces commitment without producing
coordination.

**A shared price is what converts disclosure into specialisation that pays.**
Arm C reaches the frontier *exactly* — 1.000, the competitive equilibrium,
found by agents applying a common update rule to public posts with no
auctioneer and no help from the manager.

**And then it ruins a third of its islands, permanently.** This is the result
worth the whole experiment. Arm C's ruin rate stalls at 8/12 and **never
improves however long it runs** — 15 rounds or 240, it is stuck. A shared price
tells two agents what a fair swap is; it does not make the agent holding the
only pile of fish *want* your cloth. That is the double coincidence of wants, and
no amount of agreeing on prices dissolves it. Meanwhile specialisation has turned
every agent into a one-good holder, so when settlement fails the loss is total
rather than marginal.

**The clause that fixes it is not about prices at all.** Arm D changes one
thing: the numeraire is accepted *past the point of wanting it*, because it can
be spent again. Its ruin rate falls monotonically — 12, 11, 7, 5, 3 — and its
efficiency climbs to 0.976. The two failures look identical at any single round
budget and are completely different: **C's is structural and D's is merely
slow.** That distinction is only visible because the round budget is swept
rather than chosen, which is also why quoting any single number for these arms
would have decided the result by picking the budget.

Money buys robustness and charges for it in transaction volume: arm D settles
roughly twice as many trades as arm C to get there.

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

One island per arm, one model, one seed. This is an anecdote and is reported
because it is what was actually run; the seed sweep that would make it evidence
is the expensive part. What it is good for is showing the harness works
end to end and that the interesting question — *do models invent the missing
rung?* — is live rather than foregone.

Limits, stated plainly: every agent-turn is a model call, so runs are small and
one arm of one island costs about $2 and the better part of an hour; agents take
turns rather than acting concurrently, and concurrency is where coordination is
hardest; and a single seed cannot separate an effect from a draw.

## Files

| | |
|---|---|
| `economy.py` | the island, the frontier, the certified efficiency bracket. No Switchboard, no agents — if the scorer imported the manager, a bookkeeping bug could move the frontier to meet the allocation |
| `manager.py` | the state machine and its Switchboard service |
| `traders.py` | the four scripted policies |
| `run.py` | one island end to end, over either transport |
| `llm.py` | the model-facing tool surface |
