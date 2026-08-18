"""The island: what is producible, what is preferred, and where the frontier is.

No Switchboard in this file, and no agents — this is the ground truth a run is
scored against. Keeping it separate matters: if the scorer imported the
manager, an accounting bug in the manager could move the frontier to meet the
allocation it produced, and every arm would look efficient.

The economy
-----------
``n`` agents, ``k`` goods. Each agent has one unit of labour and an
**independent production capacity** ``capacity[i][g]`` — the units of good
``g`` it would get for spending its whole unit of labour on ``g``. Capacities
are drawn independently per (agent, good), so nobody is uniformly best and
comparative advantage is not designed in by hand.

Production is a choice: agent ``i`` picks shares ``s[i][g] >= 0`` with
``sum_g s[i][g] <= 1`` and gets ``capacity[i][g] * s[i][g]`` of each good. The
labour budget is what makes this an economy rather than a free lunch — without
it every agent maxes out every good and there is nothing to trade.

Preferences are Cobb-Douglas, ``u_i(x) = prod_g x[g] ** alpha[i][g]`` with
``sum_g alpha[i][g] == 1``. The exponents summing to one is not cosmetic: it
makes utility homogeneous of degree 1, so "twice the bundle" is exactly "twice
the utility" and a *ratio* of utilities is meaningful. Every metric below is a
ratio, which is why they can be averaged across islands with different scales.

Why a frontier and not a welfare number
---------------------------------------
Any single welfare scalar picks a distribution and calls it optimal — maximise
the sum and an arm wins by immiserating one agent. The Pareto frontier is the
whole surface, so an allocation is scored on *distance to it*, which says
nothing about who should have got what. Where on the frontier an arm lands is
reported separately, because it is a different question and conventions move
the two independently.

The three benchmarks
--------------------
``autarky``   nobody trades. Closed form: a Cobb-Douglas agent alone spends
              share ``alpha[i][g]`` of its labour on good ``g``.
``planner``   the Pareto frontier itself, parameterised by welfare weights.
              Sweeping the weights over the simplex traces the whole surface.
``walras``    the competitive equilibrium: the point a perfect price mechanism
              with no transfers would reach. It is *one* point on the frontier,
              and it is the one a price convention is trying to find.

Distance to the frontier is reported as a **certified sandwich**, not an
estimate. ``efficiency`` returns a lower and an upper bound that come from two
different directions — an allocation that proves a level is reachable, and a
price vector that proves nothing better is. The gap between them is the honest
error bar on every efficiency number in this experiment, and it is reported
rather than averaged away.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

#: Frank-Wolfe steps per planner solve. The gap closes as O(1/t) and the
#: iteration is a few hundred float ops, so this is cheap insurance; the
#: residual is not assumed, it is returned as ``PlannerPoint.gap``.
_FW_STEPS = 3000

#: Below this, a good is treated as "held none of". Cobb-Douglas utility is
#: zero at a zero holding, which is a real and important outcome — an agent
#: that traded away all of something is genuinely ruined, not a rounding
#: artefact — so this only guards float noise, never a real zero.
_EPS = 1e-12


@dataclass(frozen=True)
class Island:
    """One draw of the world: who can make what, and who wants what."""

    #: ``alpha[i][g]`` — Cobb-Douglas exponent, rows sum to 1.
    alpha: tuple[tuple[float, ...], ...]
    #: ``capacity[i][g]`` — units of ``g`` from one whole unit of labour.
    capacity: tuple[tuple[float, ...], ...]

    @property
    def n_agents(self) -> int:
        return len(self.alpha)

    @property
    def n_goods(self) -> int:
        return len(self.alpha[0])

    def agent_ids(self) -> list[str]:
        return [f"a{i + 1}" for i in range(self.n_agents)]

    def good_ids(self) -> list[str]:
        return [GOOD_NAMES[g] for g in range(self.n_goods)]


#: Named rather than numbered because these end up in prompts and in a public
#: ledger. "g3" invites an agent to reason about the index; a name does not.
GOOD_NAMES = ("fish", "grain", "cloth", "timber", "salt")


def draw_island(
    n_agents: int = 6,
    n_goods: int = 5,
    *,
    seed: int = 0,
    spread: float = 0.8,
    taste: float = 1.0,
) -> Island:
    """Draw an island. ``spread`` is the log-scale of capacity dispersion.

    Capacities are lognormal and **independent across goods**, which is the
    point: no agent is handed a specialty. Comparative advantage still appears,
    because with 5 independent draws each agent is relatively good at
    *something*, but it emerges from the draw rather than from the generator
    deciding who the fisherman is.

    ``taste`` is the Dirichlet concentration on preferences. At 1.0 tastes are
    uniform over the simplex — agents differ in what they want as well as what
    they can make, so there are gains from trade even between two agents with
    identical capacities.
    """
    if not 1 <= n_goods <= len(GOOD_NAMES):
        # Goods are named, not numbered, because these end up in prompts and on
        # a public ledger. Silently running out of names would hand agents an
        # island they cannot talk about.
        raise ValueError(
            f"n_goods must be 1..{len(GOOD_NAMES)}; the named goods are "
            f"{', '.join(GOOD_NAMES)}"
        )
    if n_agents < 2:
        raise ValueError("an island needs at least two agents to have a market")
    rng = random.Random(seed)
    alpha = []
    capacity = []
    for _ in range(n_agents):
        draws = [rng.gammavariate(taste, 1.0) for _ in range(n_goods)]
        total = sum(draws)
        alpha.append(tuple(d / total for d in draws))
        capacity.append(tuple(math.exp(spread * rng.gauss(0.0, 1.0)) for _ in range(n_goods)))
    return Island(alpha=tuple(alpha), capacity=tuple(capacity))


# --- preferences ------------------------------------------------------------


def utility(alpha_i: tuple[float, ...], bundle: list[float] | tuple[float, ...]) -> float:
    """Cobb-Douglas utility. Zero if any good is missing, deliberately."""
    total = 0.0
    for a, x in zip(alpha_i, bundle, strict=True):
        if x <= _EPS:
            return 0.0
        total += a * math.log(x)
    return math.exp(total)


def mrs(alpha_i: tuple[float, ...], bundle: list[float] | tuple[float, ...]) -> list[float]:
    """Marginal rates of substitution, normalised to the first good.

    ``mrs[g]`` is how many units of good 0 this agent would just barely give up
    for one more unit of ``g``. This is the agent's *private* valuation and the
    only number it needs to publish for a price convention to work — which is
    what makes "disclose your MRS" a convention worth testing rather than a
    demand for the whole state.
    """
    marginal = [alpha_i[g] / max(bundle[g], _EPS) for g in range(len(alpha_i))]
    base = marginal[0] if marginal[0] > _EPS else _EPS
    return [m / base for m in marginal]


# --- production -------------------------------------------------------------


def output(island: Island, shares: list[list[float]]) -> list[float]:
    """Total production of each good from a labour allocation."""
    totals = [0.0] * island.n_goods
    for i in range(island.n_agents):
        for g in range(island.n_goods):
            totals[g] += island.capacity[i][g] * shares[i][g]
    return totals


def autarky(island: Island) -> tuple[list[list[float]], list[float]]:
    """Nobody trades. Returns (labour shares, utilities).

    Closed form: maximising ``sum_g alpha[g] * log(capacity[g] * s[g])`` subject
    to ``sum_g s[g] = 1`` gives ``s[g] = alpha[g]`` — the capacities drop out
    entirely. An isolated Cobb-Douglas agent spends its labour in proportion to
    its tastes and ignores what it is good at, which is exactly the loss that
    specialisation-through-trade recovers.
    """
    shares = [list(island.alpha[i]) for i in range(island.n_agents)]
    utils = []
    for i in range(island.n_agents):
        bundle = [island.capacity[i][g] * shares[i][g] for g in range(island.n_goods)]
        utils.append(utility(island.alpha[i], bundle))
    return shares, utils


# --- the frontier -----------------------------------------------------------


@dataclass(frozen=True)
class PlannerPoint:
    """One point on the Pareto frontier, plus the prices that support it."""

    weights: tuple[float, ...]
    shares: tuple[tuple[float, ...], ...]
    totals: tuple[float, ...]
    prices: tuple[float, ...]
    allocation: tuple[tuple[float, ...], ...]
    utilities: tuple[float, ...]
    #: Frank-Wolfe duality gap on the production sub-problem, in nats of the
    #: planner objective. A convergence certificate, not decoration.
    gap: float
    #: ``income[i] - weights[i]``, the budget residual. Zero for every ``i``
    #: means this frontier point is also a competitive equilibrium.
    income: tuple[float, ...]


def planner(
    island: Island,
    weights: list[float] | tuple[float, ...],
    *,
    steps: int = _FW_STEPS,
    warm: list[list[float]] | None = None,
    fixed_shares: list[list[float]] | tuple[tuple[float, ...], ...] | None = None,
) -> PlannerPoint:
    """The Pareto-optimal allocation for one vector of welfare weights.

    With ``fixed_shares`` the production plan is frozen and only the *exchange*
    is optimised — the frontier of a pure exchange economy sitting on those
    endowments. That variant is what separates the two gains from trade:
    swapping what you already made, and making different things in the first
    place. It needs no iteration at all, since total output is given.

    Solves ``max sum_i w[i] * log u_i(x_i)`` over allocations *and* production
    plans. Every Pareto optimum of this economy is the solution for some ``w``
    (Negishi), so sweeping ``w`` over the simplex traces the frontier exactly —
    there is no sampling error in *which* points are on it, only in how densely
    they are visited.

    Two facts make this cheap. Given total production ``Q``, the split is closed
    form, ``x[i][g] = w[i] * alpha[i][g] * Q[g] / A[g]`` with
    ``A[g] = sum_j w[j] * alpha[j][g]``; substituting it back leaves
    ``max sum_g A[g] * log Q[g]`` over the production polytope, which is concave
    with a trivial linear-maximisation oracle — each agent puts all its labour
    on whichever good maximises ``p[g] * capacity[i][g]``. That is Frank-Wolfe,
    and the shadow prices ``p[g] = A[g] / Q[g]`` fall out of the gradient.
    """
    n, k = island.n_agents, island.n_goods
    active = [max(w, 1e-9) for w in weights]
    a_of = [sum(active[i] * island.alpha[i][g] for i in range(n)) for g in range(k)]

    # Start from autarky rather than a corner: it is interior, so no total is
    # ever zero and the log gradient is always finite.
    default = [list(island.alpha[i]) for i in range(n)]
    shares = [list(row) for row in (default if warm is None else warm)]

    gap = float("inf")
    prices = [1.0] * k
    if fixed_shares is not None:
        shares = [list(row) for row in fixed_shares]
        steps, gap = 0, 0.0
    for t in range(steps):
        totals = output(island, shares)
        prices = [a_of[g] / max(totals[g], _EPS) for g in range(k)]
        # Linear-maximisation oracle: the corner of the polytope the gradient
        # points at. Its value over the current point is the duality gap.
        gap = 0.0
        best_good = []
        for i in range(n):
            values = [prices[g] * island.capacity[i][g] for g in range(k)]
            top = max(range(k), key=values.__getitem__)
            best_good.append(top)
            gap += values[top] - sum(values[g] * shares[i][g] for g in range(k))
        # 2/(t+3) rather than the textbook 2/(t+2): the first step must not be
        # a full jump to a corner, which would zero out every good but one and
        # put the log gradient at infinity.
        eta = 2.0 / (t + 3.0)
        for i in range(n):
            row = shares[i]
            for g in range(k):
                row[g] *= 1.0 - eta
            row[best_good[i]] += eta

    totals = output(island, shares)
    prices = [a_of[g] / max(totals[g], _EPS) for g in range(k)]
    allocation = [
        [active[i] * island.alpha[i][g] * totals[g] / a_of[g] for g in range(k)] for i in range(n)
    ]
    utils = [utility(island.alpha[i], allocation[i]) for i in range(n)]
    # What agent i's own production is worth at the supporting prices. Where
    # this equals its weight, the agent is paying for its own bundle and needs
    # no transfer — see `walras`.
    income = [
        sum(prices[g] * island.capacity[i][g] * shares[i][g] for g in range(k)) for i in range(n)
    ]
    return PlannerPoint(
        weights=tuple(active),
        shares=tuple(tuple(r) for r in shares),
        totals=tuple(totals),
        prices=tuple(prices),
        allocation=tuple(tuple(r) for r in allocation),
        utilities=tuple(utils),
        gap=gap,
        income=tuple(income),
    )


def walras(island: Island, *, rounds: int = 400, inner: int = 60) -> PlannerPoint:
    """The competitive equilibrium: the frontier point that needs no transfers.

    Under weights ``w`` the supporting prices give agent ``i`` a bundle costing
    exactly ``w[i]``, while its production is worth ``income[i]``. So the
    equilibrium is the fixed point ``w = income(w)`` — Negishi's algorithm, and
    with Cobb-Douglas it is a one-line update. Convergence is certified by the
    returned ``income`` residual, not assumed.

    This is the target of a price convention, and the interesting benchmark for
    it: reaching the frontier is one achievement, reaching *this* point on it
    without a central auctioneer is another.
    """
    n = island.n_agents
    weights = [1.0 / n] * n
    warm: list[list[float]] | None = None
    point = planner(island, weights, steps=inner, warm=warm)
    for _ in range(rounds):
        point = planner(island, weights, steps=inner, warm=[list(r) for r in point.shares])
        total = sum(point.income) or 1.0
        target = [m / total for m in point.income]
        # Damped: an undamped jump to income overshoots and can oscillate
        # between two specialisation corners without ever settling.
        weights = [0.7 * weights[i] + 0.3 * target[i] for i in range(n)]
    return planner(island, weights, steps=_FW_STEPS, warm=[list(r) for r in point.shares])


def frontier(island: Island, *, samples: int = 400, seed: int = 0) -> list[tuple[float, ...]]:
    """A point cloud on the Pareto frontier, for plotting against.

    Weights are drawn from a Dirichlet over the simplex. For two agents this is
    the familiar frontier curve; for more it is a surface, and the cloud is what
    a realised allocation gets compared to.
    """
    rng = random.Random(seed)
    n = island.n_agents
    points = []
    warm: list[list[float]] | None = None
    for _ in range(samples):
        draws = [rng.gammavariate(1.0, 1.0) for _ in range(n)]
        total = sum(draws)
        pt = planner(island, [d / total for d in draws], steps=400, warm=warm)
        warm = [list(r) for r in pt.shares]
        points.append(pt.utilities)
    return points


# --- distance to the frontier ----------------------------------------------


def _unit_cost(alpha_i: tuple[float, ...], prices: tuple[float, ...] | list[float]) -> float:
    """Cheapest bundle worth one unit of utility to this agent, at ``prices``.

    ``prod_g (p[g] / alpha[g]) ** alpha[g]``. Because Cobb-Douglas utility is
    homogeneous of degree 1, ``v`` units of utility cost exactly ``v`` times
    this — which is what turns a price vector into a hard upper bound below.
    """
    total = 0.0
    for a, p in zip(alpha_i, prices, strict=True):
        total += a * math.log(max(p, _EPS) / max(a, _EPS))
    return math.exp(total)


def _revenue(island: Island, prices: tuple[float, ...] | list[float],
             totals: tuple[float, ...] | list[float] | None = None) -> float:
    """The most the island's endowment could possibly be worth at ``prices``.

    With production free, that is every agent putting all its labour on its own
    best-paid good. With ``totals`` given, production is already fixed and the
    endowment is simply worth what it is.
    """
    if totals is not None:
        return sum(prices[g] * totals[g] for g in range(island.n_goods))
    return sum(
        max(prices[g] * island.capacity[i][g] for g in range(island.n_goods))
        for i in range(island.n_agents)
    )


@dataclass(frozen=True)
class Efficiency:
    """How close a realised allocation got to the Pareto frontier.

    ``lower`` and ``upper`` bracket the true efficiency, which is defined as
    ``1 / theta`` where ``theta`` is the largest factor by which every agent's
    utility could be scaled up at once and still be producible. 1.0 means the
    allocation is Pareto-optimal; 0.8 means every agent could have had 25% more
    of everything.

    The two bounds come from opposite directions and neither is a guess:

    * ``lower`` is witnessed by an actual feasible allocation that beats the
      realised one by that factor for *every* agent.
    * ``upper`` is witnessed by a price vector: at those prices the island
      cannot produce enough revenue to buy any more than that.

    A wide bracket means the search did not converge and the number should not
    be quoted. ``ruined`` is the separate, non-numeric failure — an agent
    holding zero of some good has zero Cobb-Douglas utility, which no scaling
    argument can rescue, and averaging it into a mean would hide the one
    outcome most worth seeing.
    """

    lower: float
    upper: float
    ruined: tuple[int, ...]

    @property
    def bracket(self) -> float:
        return self.upper - self.lower

    def __str__(self) -> str:
        if self.ruined:
            return f"ruined({','.join(str(i) for i in self.ruined)})"
        return f"{self.lower:.3f}-{self.upper:.3f}"


def efficiency(
    island: Island,
    utilities: list[float] | tuple[float, ...],
    *,
    rounds: int = 250,
    inner: int = 40,
    fixed_shares: list[list[float]] | tuple[tuple[float, ...], ...] | None = None,
) -> Efficiency:
    """Bracket the realised allocation's distance to the frontier.

    Searches welfare weights for the frontier point that lies on the ray through
    the realised utility vector. Every weight tried yields both bounds for free —
    its allocation certifies a lower one, its supporting prices an upper one —
    so the bracket tightens monotonically and is valid even if the search stops
    early.

    ``fixed_shares`` measures against the *exchange* frontier for that
    production plan instead of the whole economy's. Scoring an allocation both
    ways is what says whether an arm left goods unswapped or left the wrong
    goods made.
    """
    n = island.n_agents
    ruined = tuple(i for i, u in enumerate(utilities) if u <= _EPS)
    if ruined:
        return Efficiency(lower=0.0, upper=0.0, ruined=ruined)

    total = sum(utilities)
    weights = [u / total for u in utilities]
    theta_low, theta_high = 0.0, float("inf")
    warm: list[list[float]] | None = None
    for _ in range(rounds):
        point = planner(island, weights, steps=inner, warm=warm, fixed_shares=fixed_shares)
        warm = [list(r) for r in point.shares]
        ratios = [point.utilities[i] / utilities[i] for i in range(n)]
        # Any feasible allocation that beats theta * realised for everyone
        # proves theta is reachable.
        theta_low = max(theta_low, min(ratios))
        # ...and at these prices the island's whole endowment cannot buy more
        # than this multiple of the realised bundle.
        cost = sum(utilities[i] * _unit_cost(island.alpha[i], point.prices) for i in range(n))
        totals = point.totals if fixed_shares is not None else None
        theta_high = min(theta_high, _revenue(island, point.prices, totals) / max(cost, _EPS))
        # Shift weight onto whoever the current frontier point is shortchanging
        # relative to the target ray.
        weights = [weights[i] * (1.0 / max(ratios[i], _EPS)) ** 0.3 for i in range(n)]
        scale = sum(weights)
        weights = [w / scale for w in weights]

    # theta >= 1 always: the realised allocation is itself feasible. Efficiency
    # is its reciprocal, clamped so float noise cannot report above 1.0.
    return Efficiency(
        lower=min(1.0, 1.0 / max(theta_high, 1.0)),
        upper=min(1.0, 1.0 / max(theta_low, 1.0)),
        ruined=(),
    )


def exchange_ceiling(island: Island) -> Efficiency:
    """The best you can do by swapping only — never changing what you make.

    Freeze production at the autarky plan, let the allocation be optimal, then
    score *that* against the full economy's frontier. Everything below this line
    is a failure to swap; everything above it requires having produced
    differently. The split matters for what an agent should be talking about:
    if the ceiling is high, communication is about finding counterparties, and
    if it is low, communication is about who should make what — and no amount
    of clever haggling will recover the difference.
    """
    shares, _ = autarky(island)
    # The exchange-efficient allocation for equal weights is a competitive
    # equilibrium of the pure exchange economy; any Pareto point of it works,
    # since they all share the same production and so the same ceiling.
    point = planner(island, [1.0 / island.n_agents] * island.n_agents, fixed_shares=shares)
    return efficiency(island, point.utilities)


def capture(realised: Efficiency, autarky_eff: Efficiency) -> tuple[float, float]:
    """Fraction of the *available* gains from trade actually captured.

    Efficiency alone is hard to read — an island where autarky already scores
    0.9 has little on the table, and one where it scores 0.3 has a lot. This
    rescales so autarky is 0.0 and the frontier is 1.0, which is what makes
    numbers comparable across islands. Negative means the arm did worse than
    not trading at all, which is a real possible outcome and is not clamped.
    """
    floor = autarky_eff.lower
    if 1.0 - floor <= _EPS:
        return (1.0, 1.0)
    return ((realised.lower - floor) / (1.0 - floor),
            (realised.upper - floor) / (1.0 - floor))


@dataclass(frozen=True)
class Gains:
    """How the gains from trade were shared out, one agent at a time.

    Efficiency is deliberately *distribution-neutral*: it scales every agent by
    the same factor, so it measures how much was wasted while saying nothing
    about who got it. That separation is a virtue — but it means the second
    question needs its own answer, and this is it.

    Every number here is a ratio of an agent's realised utility to **its own**
    autarky utility, and that normalisation is not a stylistic choice. Cobb-
    Douglas utilities are not interpersonally comparable: each agent's is
    defined only up to its own monotone transformation, so a Gini or a Nash
    product over raw utilities would be arithmetic without meaning. "Agent 3
    has more utility than agent 7" says nothing. "Agent 3 ended at 0.4x what it
    would have had alone" says something true about agent 3, and the ratios can
    then be compared because they are all pure numbers against a per-agent
    baseline.

    ``below`` is the one worth watching. Voluntary trade cannot make anybody
    worse off — every settled trade passed both sides' own accept test — so an
    agent finishing under 1.0 is never a bad swap. It is a *production* bet
    placed on a price that did not materialise, which is the specific harm a
    convention can do that silence cannot.
    """

    #: ``u_i / autarky_i`` for every agent, in agent order. Kept whole so any
    #: quantile can be recovered later; a percentile computed here would
    #: degenerate at Tier 2's four agents and quietly mislead.
    ratios: tuple[float, ...]
    #: The Rawlsian number: the worst-served agent.
    worst: float
    median: float
    #: How many agents would rather not have taken part. One agent at 0.5x and
    #: six agents at 0.9x are different failures, and ``worst`` alone reports
    #: them identically.
    below: int

    @property
    def n(self) -> int:
        return len(self.ratios)

    def __str__(self) -> str:
        return f"worst {self.worst:.2f}x  med {self.median:.2f}x  below {self.below}/{self.n}"


def gains(island: Island, utilities: list[float] | tuple[float, ...]) -> Gains:
    """Each agent's outcome as a multiple of what it would have had alone.

    A ruined agent scores 0.0 and counts in ``below`` — that is not a sentinel
    here but the true ratio, since its utility really is zero and its autarky
    utility really is positive.
    """
    _, autarky_utils = autarky(island)
    ratios = tuple(u / a for u, a in zip(utilities, autarky_utils, strict=True))
    ordered = sorted(ratios)
    mid = len(ordered) // 2
    median = (ordered[mid] if len(ordered) % 2
              else 0.5 * (ordered[mid - 1] + ordered[mid]))
    return Gains(ratios=ratios, worst=ordered[0], median=median,
                 below=sum(1 for r in ratios if r < 1.0 - _EPS))
