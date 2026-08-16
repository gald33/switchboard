# The island: what has to be shared before a market works

An experiment in what agents must *agree on* — not merely be able to say — for a
market to reach its Pareto frontier, run over a Switchboard hub with a manager
that owns all the state.

```bash
python tests/experiments/barter_experiment.py --islands 12 --rounds-sweep
python tests/experiments/barter_llm_experiment.py --arms A B   # costs money
pytest tests/test_barter.py -q                                 # the gates
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

## The arms

An information ladder, removing one thing at a time.

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
unchanged and still not a model. **Arms are tool surfaces, not instructions**:
arm A has no channel tool, arm B has `say` and `listen` and *nothing about what
to put in them*. The prompt never mentions prices, numeraires, or money. If a
convention appears in arm B it was invented, and the Tier 1 ladder is what gives
it a scale.

Read a Tier 2 result against the ladder: near the exchange ceiling means the
models traded but never coordinated production; well above it means they found
something, and the kept transcript says what. The content of what agents invent
is the finding — no aggregate can carry it.

### One island, arm A (silent), Haiku 4.5, 4 agents, 5 rounds

```
  autarky floor    0.374
  exchange ceiling 0.413
  EFFICIENCY       0.337-0.339      <- below the autarky floor
  of its own plan  0.836
  worst agent      0.63x autarky
  trades           9 settled of 36 proposed  (3 rejected, 9 expired)
```

Silent models land *below not trading at all*, and one agent finished at 0.63x
what it would have had by ignoring everybody. This is the same shape as scripted
arm B and it has the same cause: they moved off the autarky production plan —
which is the best plan available to someone who cannot coordinate — and then
could not trade their way back. Note they are not bad at trading: 0.836 of the
best allocation of what they chose to make. They chose wrong, silently.

Note also that a *quarter* of proposals expired unanswered. A proposal escrows
the buyer's side, so an agent that offers into silence has locked up goods it
then cannot offer to anybody else.

One island is an anecdote, not a result. It is reported because it is what was
actually run.

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
