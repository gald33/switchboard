"""Three scripted trading policies, differing only in what agents may say.

The arms are an information ladder, and each rung is one specific claim:

    A  silent      agents may call the manager and nothing else.
    B  disclose    agents publish their marginal values on a public channel and
                   act on what they read, each in its own way.
    C  price       the same channel and the *same information* as B, plus an
                   agreed rule for turning it into one public price: a single
                   numeraire, a fixed quote format, and a shared update rule
                   everybody applies to the same posts.
    D  money       arm C plus one further clause — settle every trade in the
                   numeraire, and accept the numeraire past what you want to
                   consume, because you can spend it again.

B against C is the arm that isolates *protocol from speech*, because it is the
only comparison holding information constant. If C wins, the gain is from a
shared way of reading what was already being said, not from saying it.

C against D is the arm that isolates *unit of account from medium of exchange*,
and it exists because the first run of arm C failed in a specific and famous
way. A common price tells two agents what a fair swap is; it does not make them
want each other's goods. The agent holding the only pile of fish will not take
your cloth once it has enough cloth, however correctly the cloth is priced — so
some agents never obtain some goods at all, and under Cobb-Douglas a good you
have none of takes your utility to zero rather than merely reducing it. That is
the double coincidence of wants, and no amount of agreeing on prices dissolves
it. Arm D adds the one clause that does. The two arms share every number they
compute and differ only in what they are willing to settle in.

What this tier can and cannot show
----------------------------------
These policies are written, so a Tier 1 result cannot discover that
communication helps: it was put there. What it can do, cheaply and before any
model is involved, is three things a Tier 2 run has no way to establish about
itself:

* **Calibrate the prize.** How far apart are the arms when the policies are
  competent? A model result has no scale without that.
* **Clear the manager.** If even arm C lands far short of the competitive
  benchmark, the two-phase escrow protocol is the bottleneck and no prompt will
  fix it. That is a finding about this experiment's apparatus.
* **Locate the gains.** Whether the money is in swapping or in specialising
  decides what agents ought to be *talking about* — see ``exchange_ceiling``.

None of that tells you whether a language model, given the same channel,
invents a usable convention. That is the Tier 2 question and this file cannot
answer it.
"""

from __future__ import annotations

import math
import random
from typing import Any

from .economy import Island

#: Good 0 is the numeraire under arm C. Which good is arbitrary; that everyone
#: uses the *same* one is the entire convention.
NUMERAIRE = 0

#: Tatonnement step size for arm C's price discovery. Large enough to converge
#: in the handful of rounds a run allows, small enough not to oscillate.
_PRICE_STEP = 0.35

#: How far below its own valuation a price-quoting agent will still settle —
#: ordinary haggling slack. It is applied identically in arms B, C and D so it
#: cannot flatter one of them, and it exists because a strict inequality makes
#: the whole comparison a knife edge: under a shared price a trade at that price
#: is exactly break-even in value terms for both sides, so *any* rounding
#: decides it. With no slack at all the arms would be separated by float noise
#: rather than by their conventions.
_HAGGLE = 0.03


def gives_way(pair: list[str] | tuple[str, ...]) -> str:
    """Which of two crossed offers is withdrawn. The scripted agents' tie-break.

    The rule is *first proposed survives*, read off the trade ids, and the only
    property that matters is that both parties compute the same answer from
    information both of them have. Newest-survives would do exactly as well;
    coin-flipping would not, and neither would "whoever feels strongest",
    because the failure mode is not picking badly, it is picking *differently*.
    Two agents who both defer cancel both offers; two who both insist swap
    twice.

    Handing this to the scripted arms is deliberate and it is not a claim that
    the rule is clever. It is the control: deterministic agents are given a
    tie-break so their crossings are resolved by convention rather than by
    luck, which is what makes them a benchmark. A model agent gets the stage and
    no rule, and whether it invents one — and whether its counterparty invents
    the *same* one — is the measurement.
    """
    return max(pair, key=lambda t: int(str(t).lstrip("t") or 0))


class Floor:
    """The public channel, or the absence of one.

    Arm A gets a ``Floor`` with ``enabled=False``, which silently drops posts
    and returns nothing — so the same policy code can be run with and without
    speech and the difference is genuinely only the communication.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.posts: list[dict[str, Any]] = []
        self.sent = 0

    def post(self, agent_id: str, body: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self.sent += 1
        self.posts.append({"agent_id": agent_id, **body})

    def read(self, kind: str, *, round_no: int | None = None) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        return [p for p in self.posts
                if p.get("kind") == kind and (round_no is None or p.get("round") == round_no)]


class Trader:
    """One scripted agent. ``arm`` selects the policy; the rest is shared."""

    def __init__(self, agent_id: str, index: int, island: Island, arm: str,
                 rng: random.Random) -> None:
        self.agent_id = agent_id
        self.index = index
        self.alpha = island.alpha[index]
        self.capacity = island.capacity[index]
        self.k = island.n_goods
        self.arm = arm
        self.rng = rng
        self.goods: list[str] = []
        #: Arm C's belief about the public price. Starts at "everything is
        #: worth the same", which is what an agent with no information has.
        self.price = [1.0] * self.k

    # --- production ---------------------------------------------------------

    def declare(self, round_no: int, floor: Floor) -> None:
        """Say what you are willing to say, in the format your arm uses."""
        if self.arm == "A":
            return
        # The same underlying number in both arms: what one more unit of each
        # good is worth to me, in units of the numeraire. B says it however it
        # likes; C says it in the agreed format, on the agreed scale.
        values = self._marginal_values()
        if self.arm == "B":
            floor.post(self.agent_id, {"kind": "values", "round": round_no,
                                       "values": values, "capacity": list(self.capacity)})
        else:
            demand, supply = self._book(self.price)
            floor.post(self.agent_id, {"kind": "quote", "round": round_no,
                                       "numeraire": NUMERAIRE, "values": values,
                                       "demand": demand, "supply": supply})

    def _marginal_values(self) -> list[float]:
        """Marginal value of each good in numeraire units, at the autarky point.

        Before production there is nothing to hold, so an agent's honest opening
        valuation is the one implied by what it would consume alone. Both B and
        C disclose exactly this, which is what makes them comparable.
        """
        bundle = [self.capacity[g] * self.alpha[g] for g in range(self.k)]
        marginal = [self.alpha[g] / max(bundle[g], 1e-12) for g in range(self.k)]
        base = max(marginal[NUMERAIRE], 1e-12)
        return [m / base for m in marginal]

    def _book(self, price: list[float]) -> tuple[list[float], list[float]]:
        """What I would buy and what I would make, at ``price``.

        Income is the value of the best thing I could produce, because with
        linear technology and one unit of labour a price-taker specialises
        completely. Demand is Cobb-Douglas, so it is a fixed budget share.

        Reported as two vectors rather than their difference because the update
        rule below needs the ratio, and a good that *nobody* plans to make has
        supply zero — a fact that a single excess-demand number cannot express
        and that the price has to react to violently, or the good never appears.
        """
        best = max(range(self.k), key=lambda g: price[g] * self.capacity[g])
        income = price[best] * self.capacity[best]
        supply = [0.0] * self.k
        supply[best] = self.capacity[best]
        demand = [self.alpha[g] * income / max(price[g], 1e-12) for g in range(self.k)]
        return demand, supply

    def observe_prices(self, round_no: int, floor: Floor) -> None:
        """Arm C only: update the public price from what everyone posted.

        Nobody computes this centrally. Every agent applies the same rule to the
        same public posts and therefore lands on the same number — which is what
        a convention *is*, and why it needs no auctioneer and no manager
        support. If one agent used a different step size the prices would
        diverge and the arm would degrade to B, which is the honest failure mode
        of a convention nobody quite shares.
        """
        if self.arm not in ("C", "D"):
            return
        quotes = floor.read("quote", round_no=round_no)
        if not quotes:
            return
        demand = [0.0] * self.k
        supply = [0.0] * self.k
        for quote in quotes:
            for g in range(self.k):
                demand[g] += quote["demand"][g]
                supply[g] += quote["supply"][g]

        # Textbook tatonnement: bid up what is over-subscribed. The ratio form
        # matters more than it looks. Planned supply is bang-bang -- every agent
        # names one good -- so a good nobody names has supply exactly zero, and
        # only a rule that divides by supply reacts strongly enough to make
        # somebody switch to it. A difference-based rule leaves the good
        # unproduced, and a good nobody makes is one nobody can ever buy: with
        # Cobb-Douglas that is not a small loss, it is everybody at zero.
        # Every good is repriced, the numeraire included, and the whole vector is
        # then rescaled so the numeraire reads 1. Holding the numeraire's price
        # *fixed* instead looks equivalent and is not: it is the one good whose
        # scarcity can then never push its own relative price up, so if nobody
        # happens to produce the numeraire, nothing ever makes anybody start.
        # Only prices relative to it are meaningful, so rescaling loses nothing.
        step = _PRICE_STEP / (1.0 + round_no / 15.0)
        for g in range(self.k):
            ratio = demand[g] / max(supply[g], 1e-6)
            self.price[g] *= min(max(ratio, 1e-3), 1e3) ** step
        base = max(self.price[NUMERAIRE], 1e-12)
        for g in range(self.k):
            self.price[g] = min(max(self.price[g] / base, 1e-4), 1e4)

    def production_plan(self, floor: Floor) -> dict[str, float]:
        """How to split one unit of labour. This is where the arms diverge most.

        Arm A has nothing to go on but its own tastes, and a Cobb-Douglas agent
        alone spreads labour in proportion to them — it never specialises,
        because specialising is only worth it if you can trade the surplus.
        Arms B and C have a price to produce against, so they concentrate.
        """
        if self.arm == "A":
            return {self.goods[g]: self.alpha[g] for g in range(self.k)}

        if self.arm == "B":
            # No agreed price, so each agent forms its own from the disclosures
            # by averaging them. Reasonable, unilateral, and — because everyone
            # averages a slightly different set of posts and then acts alone —
            # not the same number for everybody.
            posts = floor.read("values")
            if not posts:
                return {self.goods[g]: self.alpha[g] for g in range(self.k)}
            price = [
                sum(p["values"][g] for p in posts) / len(posts) for g in range(self.k)
            ]
        else:
            price = self.price

        best = max(range(self.k), key=lambda g: price[g] * self.capacity[g])
        if self.arm == "B":
            # Full specialisation is the right answer only if you are sure of
            # the price you will trade at. B is not sure of it -- nobody agreed
            # on one -- so it hedges, which is the cost of having no convention
            # rather than a flaw in the policy.
            plan = {self.goods[g]: 0.5 * self.alpha[g] for g in range(self.k)}
            plan[self.goods[best]] = plan.get(self.goods[best], 0.0) + 0.5
            return plan
        return {self.goods[best]: 1.0}

    def production_instalment(self, holdings: list[float], floor: Floor) -> dict[str, float]:
        """How to split *this round's* instalment, given what I already hold.

        The one-shot plan is a bet placed before any goods exist: it can only be
        read off tastes and a believed price. An instalment is placed after some
        trading has happened, so there is a second thing to read — what the
        market actually gave you. That is the entire content of rolling labour
        in Tier 1, and it is deliberately the *only* difference: no extra
        messages are sent and no extra prices are formed, so a rolling island
        differs from a one-shot one in when labour is committed and in nothing
        else. Anything the comparison shows is about timing.

        Before anything has been produced there is nothing to learn from, so the
        first instalment is exactly the one-shot plan — which is what makes
        "rolling with one instalment" identical to "once" rather than merely
        similar. After that:

        * with a price, make the most valuable thing you are still short of, and
          fall back to the most valuable thing you can make if you are short of
          nothing. Producing more of what the market has already handed you is
          the specific loss a one-shot bet has no way to avoid.
        * without one, make whatever raises utility fastest per unit of labour,
          ``capacity[g] * du/dx[g]``. That is the best a silent agent can do and
          it needs nobody's cooperation — and because it is greedy toward the
          same optimum, many small instalments of it approach the alpha split
          the one-shot silent plan names outright.
        """
        if sum(holdings) <= 1e-9:
            return self.production_plan(floor)

        if self.arm == "A":
            gain = [self.capacity[g] * self.alpha[g] / max(holdings[g], 1e-9)
                    for g in range(self.k)]
            return {self.goods[max(range(self.k), key=lambda g: gain[g])]: 1.0}

        price = self.price if self.arm in ("C", "D") else self._own_price
        _, gap = self.wants(holdings)
        short = [g for g in range(self.k) if gap[g] > 1e-6]
        pool = short or list(range(self.k))
        best = max(pool, key=lambda g: price[g] * self.capacity[g])
        return {self.goods[best]: 1.0}

    # --- trading ------------------------------------------------------------

    def wants(self, holdings: list[float]) -> tuple[list[float], list[float]]:
        """Target bundle and the gap to it, in the arm's own terms."""
        if self.arm == "A":
            # No price to value a bundle at, so the target is simply "more of
            # whatever I have least of relative to how much I want it".
            return holdings, [0.0] * self.k
        price = self.price if self.arm in ("C", "D") else self._own_price
        wealth = sum(price[g] * holdings[g] for g in range(self.k))
        target = [self.alpha[g] * wealth / max(price[g], 1e-12) for g in range(self.k)]
        return target, [target[g] - holdings[g] for g in range(self.k)]

    _own_price: list[float]

    def adopt_own_price(self, floor: Floor) -> None:
        posts = floor.read("values")
        if posts:
            self._own_price = [sum(p["values"][g] for p in posts) / len(posts)
                               for g in range(self.k)]
        else:
            self._own_price = [1.0] * self.k

    def accepts(self, holdings: list[float], give_to_me: dict[str, float],
                take_from_me: dict[str, float]) -> bool:
        """Would I approve this trade? The rule an arm can *afford* to use.

        This is where the arms differ most consequentially, and the difference
        is forced rather than chosen.

        Arm A has no price, so the only test available to it is whether the
        swap raises its utility right now. That test has a hard failure mode:
        Cobb-Douglas utility is zero whenever *any* good is missing, so an agent
        holding none of two goods gains nothing measurable from a trade that
        fixes only one of them. Every single swap scores zero-improves-to-zero
        and the agent refuses all of them. An agent that has specialised cannot
        unwind, and cannot discover that by trying.

        A price fixes exactly that, and this is the sharpest thing a convention
        buys: it makes a trade evaluable when marginal utility is not. B and C
        accept anything that is worth at least what it costs at their price and
        moves them nearer the bundle they are aiming at — a test that works fine
        from a standing start of zero.

        The catch is that the price has to be right. Accepting on value rather
        than on realised utility means an agent can be talked into a trade that
        leaves it worse off than never having traded, if the price it believes
        is wrong. That is what ``worst_ratio`` in the results is watching for,
        and it is a genuine risk the silent arm does not run.
        """
        after = list(holdings)
        for good, qty in give_to_me.items():
            after[self.goods.index(good)] += qty
        for good, qty in take_from_me.items():
            after[self.goods.index(good)] -= qty
        # Non-negativity only. The tempting stronger guard -- refuse anything
        # that leaves a zero anywhere -- looks prudent and is fatal: an agent
        # that specialised holds one good and four zeros, so *every* first trade
        # leaves some zero behind, and the guard would forbid all of them and
        # strand the agent on its own pile forever. Whether a zero is acceptable
        # is a question about value, and each arm answers it below with whatever
        # it has.
        if min(after) < -1e-9:
            return False

        if self.arm == "A":
            return _utility(self.alpha, after) > _utility(self.alpha, holdings) * (1 + 1e-9)

        price = self.price if self.arm in ("C", "D") else self._own_price
        value_in = sum(price[self.goods.index(g)] * q for g, q in give_to_me.items())
        value_out = sum(price[self.goods.index(g)] * q for g, q in take_from_me.items())
        if value_in < value_out * (1 - _HAGGLE):
            return False
        # ...and it must actually move me toward the bundle I want. At the
        # posted price a fair swap leaves wealth unchanged, so the target is a
        # fixed point and this distance genuinely shrinks toward it.
        target, _ = self.wants(holdings)
        # Arm D's whole content is this exclusion. Money is scored out of the
        # "am I nearer what I want" test, so an agent takes it past its own
        # appetite -- which is precisely what makes it money rather than just
        # another good with a published price. Under arm C the numeraire counts
        # like anything else, so an agent full of fish stops accepting fish, and
        # whoever still needs cloth from it is simply stuck.
        scored = [g for g in range(self.k)
                  if not (self.arm == "D" and g == NUMERAIRE)]
        before_gap = sum(price[g] * abs(target[g] - holdings[g]) for g in scored)
        after_gap = sum(price[g] * abs(target[g] - after[g]) for g in scored)
        return after_gap < before_gap - 1e-12


def _utility(alpha: tuple[float, ...], bundle: list[float]) -> float:
    total = 0.0
    for a, x in zip(alpha, bundle, strict=True):
        if x <= 1e-12:
            return 0.0
        total += a * math.log(x)
    return math.exp(total)


def propose_for(trader: Trader, holdings: list[float], peers: list[Trader],
                peer_holdings: dict[str, list[float]], rng: random.Random
                ) -> tuple[str, dict[str, float], dict[str, float]] | None:
    """Pick a counterparty and an offer, in the arm's style. ``None`` to pass.

    Every arm ends up calling the same manager operation with the same shape of
    argument. What differs is who it picks and at what rate — which is exactly
    the thing communication is supposed to improve.

    Whatever the arm decides, the offer is put through the proposer's *own*
    accept test before it leaves. That guard is load-bearing rather than
    defensive. Offers are sized off marginal rates, and a marginal rate is only
    accurate for an infinitesimal trade — Cobb-Douglas curves away underneath a
    large one, so a bundle priced at exactly the rate an agent quotes can still
    leave it worse off than not trading. The buyer's side is escrowed the moment
    it proposes and it never gets to reconsider, so the check has to happen here
    or not at all. With it, both sides have passed their own test before any
    goods move, and "did anyone end up worse off than never trading" becomes a
    question about the *convention* rather than about arithmetic.
    """
    offer = _draft(trader, holdings, peers, peer_holdings, rng)
    if offer is None:
        return None
    _, give, want = offer
    if not trader.accepts(holdings, give_to_me=want, take_from_me=give):
        return None
    return offer


def _draft(trader: Trader, holdings: list[float], peers: list[Trader],
           peer_holdings: dict[str, list[float]], rng: random.Random
           ) -> tuple[str, dict[str, float], dict[str, float]] | None:
    """The arm-specific part: who to approach and at what rate."""
    surplus = [g for g in range(trader.k) if holdings[g] > 1e-6]
    if not surplus:
        return None

    if trader.arm == "A":
        # Blind: offer some of whatever is cheapest to me for whatever is
        # dearest, to a peer picked at random, at a rate drawn around my own
        # indifference point. This is a random search for a gain from trade and
        # it is the honest null -- it is what is left when nothing can be said.
        rates = [trader.alpha[g] / max(holdings[g], 1e-12) for g in range(trader.k)]
        sell = min(surplus, key=lambda g: rates[g])
        buy = max(range(trader.k), key=lambda g: rates[g])
        if sell == buy:
            return None
        peer = rng.choice([p for p in peers if p.agent_id != trader.agent_id])
        qty = holdings[sell] * rng.uniform(0.05, 0.35)
        # My own indifference rate: this much of `buy` exactly replaces the
        # `qty` of `sell` I am giving up.
        fair = rates[sell] / max(rates[buy], 1e-12)
        # Ask for *more* than indifference, never less. The buyer's side of a
        # trade is committed at propose time and it never gets a second look, so
        # a proposal below its own indifference is a loss it has already agreed
        # to. Asking above it means every trade this agent proposes is one it
        # gains from, and since the seller approves only what it gains from too,
        # no settled trade can leave either side worse off. How far above is a
        # blind guess -- ask too much and nobody accepts, which costs a round
        # and nothing else.
        ask = qty * fair * rng.uniform(1.05, 1.9)
        if qty <= 1e-9 or ask <= 1e-9:
            return None
        return peer.agent_id, {trader.goods[sell]: qty}, {trader.goods[buy]: ask}

    _, gap = trader.wants(holdings)
    price = trader.price if trader.arm in ("C", "D") else trader._own_price

    if trader.arm == "D":
        # Every trade has money on one side, so a counterparty only has to want
        # *money*, which everybody does. Sell a surplus for money when holding
        # little, spend money on the biggest shortfall otherwise. That
        # alternation is the whole policy -- there is no partner search left,
        # because the double coincidence of wants has been dissolved rather
        # than searched around.
        cash = holdings[NUMERAIRE]
        shortfalls = [g for g in range(trader.k) if g != NUMERAIRE and gap[g] > 1e-6]
        surpluses = [g for g in range(trader.k)
                     if g != NUMERAIRE and gap[g] < -1e-6 and holdings[g] > 1e-9]
        buying = shortfalls and cash > 1e-6 and (not surpluses or rng.random() < 0.5)
        if buying:
            need = max(shortfalls, key=lambda g: price[g] * gap[g])
            holders = [p for p in peers
                       if p.agent_id != trader.agent_id and peer_holdings[p.agent_id][need] > 1e-9]
            if not holders:
                return None
            peer = rng.choice(holders)
            qty = min(gap[need], peer_holdings[peer.agent_id][need],
                      cash * price[NUMERAIRE] / max(price[need], 1e-12)) * 0.9
            if qty <= 1e-9:
                return None
            return (peer.agent_id,
                    {trader.goods[NUMERAIRE]: qty * price[need] / max(price[NUMERAIRE], 1e-12)},
                    {trader.goods[need]: qty})
        if not surpluses:
            return None
        sell = min(surpluses, key=lambda g: price[g] * gap[g])
        holders = [p for p in peers
                   if p.agent_id != trader.agent_id and peer_holdings[p.agent_id][NUMERAIRE] > 1e-9]
        if not holders:
            return None
        peer = rng.choice(holders)
        qty = min(-gap[sell], holdings[sell]) * 0.9
        cash_asked = qty * price[sell] / max(price[NUMERAIRE], 1e-12)
        if qty <= 1e-9 or cash_asked > peer_holdings[peer.agent_id][NUMERAIRE]:
            cash_asked = peer_holdings[peer.agent_id][NUMERAIRE] * 0.9
            qty = cash_asked * price[NUMERAIRE] / max(price[sell], 1e-12)
        if qty <= 1e-9 or cash_asked <= 1e-9:
            return None
        return peer.agent_id, {trader.goods[sell]: qty}, {trader.goods[NUMERAIRE]: cash_asked}

    sell = min(range(trader.k), key=lambda g: gap[g])
    buy = max(range(trader.k), key=lambda g: gap[g])
    if gap[sell] >= -1e-6 or gap[buy] <= 1e-6 or holdings[sell] <= 1e-9:
        return None

    if trader.arm == "C":
        # The convention says trade at the posted price, so the only thing left
        # to choose is a counterparty with the mirror position. Everyone reads
        # the same price, so an offer at it is one the other side can check
        # against its own books and answer immediately.
        candidates = [
            p for p in peers
            if p.agent_id != trader.agent_id
            and peer_holdings[p.agent_id][buy] > 1e-6
            and p.wants(peer_holdings[p.agent_id])[1][buy] < 0
        ]
        if not candidates:
            return None
        peer = rng.choice(candidates)
        limit = min(-gap[sell], holdings[sell],
                    min(peer_holdings[peer.agent_id][buy],
                        gap[buy]) * price[buy] / max(price[sell], 1e-12))
        if limit <= 1e-9:
            return None
        qty = limit * 0.9
        return (peer.agent_id, {trader.goods[sell]: qty},
                {trader.goods[buy]: qty * price[sell] / max(price[buy], 1e-12)})

    # Arm B: a counterparty chosen from the disclosures -- whoever values what I
    # am selling most relative to what I want -- but the *rate* still has to be
    # guessed, because two agents who each averaged the floor their own way do
    # not hold the same price.
    best_peer, best_gain = None, 0.0
    for peer in peers:
        if peer.agent_id == trader.agent_id or peer_holdings[peer.agent_id][buy] <= 1e-6:
            continue
        theirs = peer._marginal_values()
        gain = theirs[sell] / max(theirs[buy], 1e-12) - price[sell] / max(price[buy], 1e-12)
        if gain > best_gain:
            best_peer, best_gain = peer, gain
    peer = best_peer or rng.choice([p for p in peers if p.agent_id != trader.agent_id])
    qty = min(-gap[sell], holdings[sell]) * rng.uniform(0.4, 0.9)
    if qty <= 1e-9:
        return None
    # At or above my own valuation, for the same reason as arm A: the buyer's
    # side is escrowed on proposal, so anything below it is a committed loss.
    rate = price[sell] / max(price[buy], 1e-12)
    return (peer.agent_id, {trader.goods[sell]: qty},
            {trader.goods[buy]: qty * rate * rng.uniform(1.0, 1.2)})
