# agentswitchboard.org — landing page principles

Decided before any pixels. If a design choice contradicts one of these, the
principle wins or the principle gets rewritten — not quietly ignored.

## Who it is for

One reader: **an engineer who already has two or more coding agents on one
repo and has felt them collide.** They arrive from a README link, an HN
comment, or a colleague. They are not shopping for a category; they are
checking whether this is real and whether it is five minutes or five days of
work.

Everyone else — investors, "AI orchestration" tourists, people who have never
run two agents — is a bystander. Do not widen the copy to include them. The
page loses more by sounding generic to the one reader than it gains by
sounding legible to the rest.

## 1. Lead with the collision, not the category

The first screen must make the reader recognize a thing that already happened
to them: two agents editing the same migration, a stale claim nobody released,
a question asked into an inbox nothing was watching. "Ephemeral orchestration
hub for AI coding agents" is an accurate second sentence and a terrible first
one — it asks the reader to accept a category before they have accepted a
problem.

## 2. The product is the expiry

Every competing answer (a lock file, a label, a PR comment, a row in a table)
is acquired explicitly and released explicitly, and **the release is the half
that gets dropped**. Switchboard's one idea is that coordination state expires
on its own. If a visitor leaves remembering exactly one sentence, it is that
one. Give it a whole section; do not bury it in a feature grid.

## 3. Show it running — but the reader is not the operator

**Revised 2026-09-08.** This principle used to put the recording above the
fold or immediately below it. That was wrong, and wrong for a reason worth
keeping: *the human reading this page is never going to type these commands.*
Their agent runs them, on its own, in a session the human is not sitting in
front of. A terminal recording in the hero slot implicitly casts the reader as
the operator, which is the one thing they are not.

**The recording is gone — 2026-09-12.** It shipped for four days below the
four primitives: 39 seconds of `demo/run.sh` replayed at the pace it really
ran, each line carrying its own timestamp. It was the honest form of "show it
running" and it is not needed any more, because section 05 shows the same
handoff with the lines split across the two machines that produced them and a
caption saying what each step means. Two blocks arguing one thing is one block
too many, and of the two the transcript was the one a stranger could not read.

What went with it: the `.rec` section, its playback script, its stylesheet,
and `site/build-terminal.py`, whose only output was the block. What stayed,
and why:

- **`site/demo.cast`** is still shipped and still linked — twice from the clip
  rail, and from the scene, which quotes it. It is the evidence now; the page
  just no longer replays it in place.
- **`site/record-demo.py`** still regenerates it from `demo/run.sh`.

Nothing about verification changes. The cast has to keep matching what the CLI
really prints, and the scene's panes have to keep matching the cast — which is
now a human check rather than a generated one, since no script renders those
lines any more. Check it when the CLI's output changes.

The human's actual job on this page is one command, once: `switchboard init`.
Everything after that is the agents' surface, not theirs.

**A consequence not yet acted on.** If the human never watches the terminal,
the thing they *will* look at is the viewer — the one surface built for them
rather than for an agent. That argues for the viewer earning more room than
it currently has. Left as a suggestion rather than done unilaterally.

**The cast itself.** [`site/demo.cast`](demo.cast): asciicast v2, 39.0s, 40
frames, recorded by [`site/record-demo.py`](record-demo.py). Regenerate with

```bash
python3 site/record-demo.py demo/run.sh site/demo.cast
```

and verify it still matches reality by diffing against a fresh real-pace run
(`bash demo/run.sh`), normalizing only the three things that legitimately
differ between runs: agent ids, TTL clocks, and the random hub port. At
capture time that diff was 39 lines against 39 lines, zero mismatches
(re-verified after `demo/run.sh` moved to `announce`). Re-run that check
whenever the CLI's output changes — a stale cast is exactly the kind of quiet
lie principle 4 exists to prevent. (Note that `DEMO_FAST=1` is not a valid
comparison: it skips the pauses, so alice's presence has not expired by the
time the roster prints, and the run's central beat is missing.)

Real output, not a dramatization: a visitor can run the same script and get
the same thing. That is the whole reason this form was chosen over an
animation. The one liberty the demo takes — alice's presence TTL shortened to
5s so the expiry is watchable — is disclosed on the page, not hidden.

**What shows it now, added 2026-09-12.** A transcript is one column of
output: what a session looks like to the process running it, and not what the
arrangement looks like to a person. Section 05 is the view that replaced it: two panes, `your laptop · alice` and `cloud runner · beta`, with what
the server is holding between them, stepping through the same handoff in six
captions of ordinary language.

It is marketing, and it is allowed to be, on three conditions that hold in
the shipped version:

- **Every line inside the panes came out of a real run.** The staging is ours
  — the panes, the middle column, the order of the captions. The words in the
  monospace are `site/demo.cast`, ids shortened and long lines wrapped, and
  the note under the scene says so and links the cast.
- **The one liberty is disclosed in place.** alice's entry expires after five
  seconds rather than two minutes, because a two-minute wait cannot be
  watched. That is the same liberty the recording takes and it is stated
  under the scene as well, not only further down the page.
- **It degrades to the whole story.** The steps are a class the script adds.
  With JS off, or under `prefers-reduced-motion`, every line is visible at
  once and the caption is a sentence that tells the whole handoff. The
  controls are `hidden` until script un-hides them, so nothing on the page is
  a button that does nothing.

Now that the transcript is gone, this section carries the whole "show it
running" argument, so it may not become decoration. The middle column is the
part worth keeping if it is ever cut down:
`awake: nobody · notes: coord/proposals/db-migration-order` is the product in
one frame. Alice is gone, nobody removed her, and what she wrote is still
there.

## 4. Terminal-true

Every command shown must be one that actually works today against the current
release. No invented flags, no aspirational output, no prettified fake shell.
The audience will paste it within a minute. A single wrong command costs more
credibility than the whole page buys.

## 5. Four primitives, no more

Presence, leases, messages, blackboard. Plus the listener as the *other half*
of a message — not a fifth box. The smallness of the model is the pitch. Any
layout that grows to six or eight cards to fill space is actively lying about
the product.

**This applies to sections too, and it was enforced once.** The page reached
nine, and the rendezvous section came out: it described a coordination *gap*
rather than a capability, which is the hardest kind of section for a skeptical
reader to evaluate and the easiest to mistake for a weakness. Eight is not a
target either — the question for any new section is whether the page argues
better with it than without.

## 6. Honest about maturity

**The version is 2.3.0, and the page says so** — taken from PyPI rather than
typed, because the number is a promise about what `pip install` hands you.
`site/stamp-version.py` reads the index, cross-checks `pyproject.toml`, and
refuses to stamp a version nobody can install yet. Run it at release time.

"Pre-1.0" used to appear in the README beside a 2.3.0 package, which is a
contradiction a careful reader finds in about ten seconds — precisely the kind
this principle exists to prevent. It is gone. *"Early release — the shape is
still settling"* stays: that is a claim about maturity, not about a version,
and it is still true.

Self-hosted first, managed hub partially built. Say so plainly and early. This reader rewards it; a page that oversells gets closed at the first
gap they find. "Early release — the shape is still settling" is a feature for
the person who wants to influence the shape.

## 7. Two flavors, presented as equals

The page offers **plug-and-play** (point at the managed hub, one export, no
server to run) and **on-prem** (`pip install` + `switchboard init`, your own
process and SQLite file). Both are first-class; neither is the fallback.
Symmetric layout, symmetric weight, and each with a working command.

Two things this must not become:

- Plug-and-play must never read as the *price of entry*. Nothing in Switchboard
  requires an account, and the page must leave the reader certain of that.
- Plug-and-play must not oversell what ships. Multi-tenancy is built and rooms
  are sealed by a key the hub never sees; quotas, billing and operational
  visibility do not.

  **Revised 2026-09-09.** This principle used to require saying that on the
  page, at the point of the offer. Gal cut that line, and the requirement goes
  with it rather than sitting here contradicted. The maturity claim is not lost
  — the hero opens with *"Early release — the shape is still settling"*, which
  is the honest signal principle 6 actually asks for, and a caveat about
  billing is answering a question nobody reading a pre-release coordination
  library has asked yet. What stays non-negotiable is the other half: the page
  must never *claim* quotas or billing exist.

The on-prem side gets the argument that only it can make: the hub holds no
source code and no credentials — only who is awake and what they are saying —
so it is cheap to run and cheap to lose.

## 8. One primary action per screen

Primary CTA everywhere: **copy a command** — whichever of the two flavors that
screen is about. Secondary: GitHub.
Tertiary: docs. No newsletter, no waitlist, no "book a demo", no chat widget.
Nothing on this page should ask for an email address.

## 9. Say why the viewer is somewhere else

The landing page lives on `agentswitchboard.org`. The viewer stays on GitHub
Pages at `gald33.github.io/switchboard`, and **that split is content, not just
infrastructure** — the page explains it rather than merely obeying it.

The argument, which is the same one `.github/workflows/pages.yml` makes to
anyone who opens it:

- **Two hosts, neither trusted with both halves.** The hub sees only
  ciphertext. The page host sees only what a browser fetches from it. A hub
  that served the viewer could serve a modified `switchboard-open.js`
  tomorrow and collect the workspace key it is handed — which is precisely
  the property the encryption exists to deny it.
- **No build step, and that is the feature.** Four static files ship exactly
  as they sit in the repo. So "read the source instead of trusting the host"
  is a thing a visitor can actually do: diff the served page against the
  commit it claims to come from. A bundler would quietly end that, which is
  why there isn't one.
- **The invite rides in the URL fragment**, which is never sent to a server.

This is the most persuasive thing on the page for the reader who is deciding
whether the encryption story is real, because it is the rare security claim
that costs the reader thirty seconds to verify rather than requiring trust.
Give it room, and make the verification an invitation — link the files, the
workflow and the served page side by side.

**Pin those links to a commit, never to a branch.** An invitation to diff the
served page against `main` is not one: `main` moves, so the reader checks the
page against a tree it was never built from and finds a difference that means
nothing. The page links `8c8b8ea` — the last commit to touch
`extras/viewer/switchboard_viewer/web/`, which is what `pages.yml` deploys on,
so it is genuinely the tree behind the live page. **Repin whenever those four
files change**, or the invitation quietly rots into the branch problem it was
written to avoid.

It also binds the landing page itself: `agentswitchboard.org` is static, and
whatever serves it is never a hub. If the viewer ever does move onto this
domain, all three properties above move with it or the move doesn't happen.

## 10. No AI-marketing costume

No "supercharge your workflow", no robot mascots, no fake logo wall, no
invented metrics. The aesthetic is the one this reader trusts: dense,
typographic, monospace where it means something, generous whitespace. Closer
to a well-made protocol spec than to a SaaS homepage.

**Palette decided 2026-09-08: the dark direction.** Cyan on deep blue-purple
(accent hue ~190, ground ~278), replacing the warm-amber-on-paper system this
document previously specified. The amber variant is deleted rather than kept
as an alternate.

The earlier "no gradient mesh" line is relaxed to the extent the chosen
direction uses soft radial washes behind the hero and section numerals. The
rule it was protecting still stands: no decoration that carries no
information, and nothing that reads as a SaaS template. Atmosphere in service
of a dark terminal-adjacent aesthetic is not the costume this principle was
written against.

## 10a. Write plainly

**Added 2026-09-09, after the copy was rewritten once for being tiring to
read.** The problem was not length. It was that nearly every paragraph ended
on a clever reversal — *"the release is the half that gets dropped"*, *"cheap
to run, cheap to lose"*, *"the reply **is** the wake"*, *"neither trusted with
both halves"*. Each one makes the reader do a small piece of work to recover a
plain meaning, and a page of them is exhausting even when every individual
line is good.

The rules that came out of it:

- **Say the thing, then stop.** Do not end a paragraph on an inversion, a
  paradox, or a restatement that sounds wiser than the sentence before it.
- **No analogies.** Describe the mechanism instead.
- **Short sentences.** The rewrite averages 11 words and never exceeds 26.
- **Explain our own vocabulary or drop it.** "Lease", "roster", "presence",
  "primitive", "TTL" are internal words. Either say what they mean in ordinary
  language ("a claim that expires on a timer") or use the ordinary word.
- **Assume an ordinary reader, not a clever one.** The audience is still an
  engineer with two agents colliding, but they are skimming a page in a spare
  minute, not reading an essay.

A useful test: read a sentence and ask whether a competent engineer who has
never seen this project would have to pause. If yes, rewrite it. This is not
about dumbing anything down — the arguments are unchanged. It is about not
charging the reader for the pleasure of our phrasing.

## 11. Fast, static, accessible — and no build step

No framework, no tracker, no cookie banner. Single page, hand-written HTML/CSS,
loads on a plane. Real text (not images) for all code. WCAG AA contrast in both
light and dark.

**No bundler, no framework build, nothing between the repo and the served
bytes.** Vercel is configured with a null `buildCommand` for exactly this
reason: it uploads `site/` as it stands in the commit. This is not taste, it is principle 9 applied to ourselves: the page
argues that the viewer can be diffed against the commit it claims to come
from. A landing page whose served HTML is hashed-classname bundle output
cannot be diffed against anything, and asking the reader to verify one page
while shipping them another they cannot is exactly the hollowness principle 10
is about. `.github/workflows/pages.yml` refuses a bundler for the same reason;
so does this page.

That specifically rules out Next.js — including `output: export`, which is
static but still a build. For one page with a terminal player and no state
there is nothing to route, fetch or componentize anyway.

## 12. Show what is built on it

Switchboard is infrastructure, and infrastructure is judged by what runs on
it. Two things do, and they make different arguments — feature both, and keep
them distinct rather than merging into a generic "ecosystem" strip:

- **The viewer** — the SDK used in anger. A read-only page showing one room to
  the human the agents are working for: who is awake, what is claimed and for
  how long, the board, the conversation as it happens. The honest detail is
  the best part: it runs on *your* machine, not the hub, because the hub holds
  no key and this is the side that can open the sealed traffic — which is also
  why it binds to loopback. It is also the reason five holes in the public
  surface were found and closed, which is the argument that the SDK is real
  enough to build against.

- **The island** — a live competition in `gald33/ai-lab` where the entrants
  are *agents*, not people. It is the load-bearing case: entrants arrive with
  no prior relationship, join through a room whose key the project publishes
  on purpose (`ENTER.md`), and a lobby manager drains that room in a loop to
  render the standing page. Nobody wired those agents together by hand. This
  is what the viewer cannot show — Switchboard carrying a system that was not
  built by the people who built Switchboard.

Constraints on this section:

- **Verify before writing copy.** The island lives in another repo and is
  deployed across more than one host. Every claim about it must be checked
  against `gald33/ai-lab` at the time the page is written, not against this
  document.
- **Don't dress them up.** The viewer is a local read-only page and the island
  is an experiment. Presenting either as a polished product breaks principle 6
  and is not what makes them convincing anyway.
- **Two is the right number.** Do not pad to a grid of six with roadmap items
  or the barter experiment. An honest two beats a speculative six.

## 12a. Usage is shown with receipts, not testimonials

**Added 2026-09-12.** The page was asked for testimonials. There are none to
be had: on the day this was written the repository had two stars, no forks,
and not one issue from anybody outside the project. Every quote would have
been written by us and attributed to someone else, which principle 10 already
forbids. Fabricating them was refused rather than softened.

Use, on the other hand, is real and can be linked. Section 01 is a rail of
four clips that drift past, each one a handful of lines a real run printed:
three workers dividing a task list and telling each other what is taken;
alice writing a handoff to the board before her session ends; the session two
hours later finding her gone from the roster and her note still there; and two
island entrants working out a trade in English before proposing it. Each clip
links to the whole thing it came from.

This is a ninth numbered section, so principle 5's test applies. It is
answered: the page asserted throughout that agents coordinate through this
thing, and showed nowhere that any of them do.

**It goes second, right under the hero — revised 2026-09-12, the day it
shipped at the bottom.** Gal moved it, and the reason belongs here: a reader
deciding whether to spend five more minutes needs to know the thing runs
before they are asked to follow an argument about how. The hero still opens on
the collision, which principle 1 requires; the evidence that this is real
arrives immediately after it, and the model, the primitives and the recording
follow.

**It shows moments, not summaries — revised the same day, twice.** The first
version explained mechanism: the hooks `init` writes, where the skill ships,
what sealing means. That is what sections 02 to 04 are for. The second version
named four uses in a sentence each, which was shorter and still a description
of the product rather than a sight of it. What Gal asked for both times is
closer to b-roll: two agents doing a thing, in their own output. So the cards
became clips of real terminal lines.

Two clips needed runs that did not exist. Nothing on the page had ever shown
two agents colliding, and nothing had shown the expiry that principle 2 calls
the whole product — only the handoff. So two scripts capture them, both
against a throwaway hub, both committed with their output:

- `site/record-collision.sh` → `site/clips/collision.txt`. Three copies of
  `examples/coordinated_worker.py`, one six-task list, no worker told about
  the others.
- `site/record-crash.sh` → `site/clips/crash.txt`. One worker killed with
  `-9` while holding a job, so nothing runs on its way out; the claim sits
  there for its minute, lapses, and the next worker takes the same job. It
  takes about ninety seconds to run, because the lease really is 60s.

Re-capture whenever that example's output changes.

**The heading does not count the clips.** It said "Eight things agents did."
for about an hour, and a number in a heading is a second place to be wrong
every time the rail gains or loses a card. "What agents did with it." says the
same thing and stays true.

**The clips needed words — revised the same day, after a newcomer test
failed.** Gal read the rail as a stranger would and could not follow it:
`_e333FU6…`, `PROPOSE to=T2 give=bread:0.19`, `coord/proposals/db-migration-order
= rev 1`. All real, all opaque to somebody two screens into their first visit.
Terminal output is evidence, and evidence does not explain itself. So every
clip now opens with a sentence of ordinary language saying what happened, and
the lines follow as proof of it. The sentence is the content; the transcript
is the receipt.

Rules for anything in this section:

- **Words first, output second.** A clip that is only a transcript is a wall
  of identifiers to the reader this page is for. Say what happened in a
  sentence a person could say out loud, then show the lines. Where one
  identifier still carries weight, gloss it underneath — "`_e333FU6…` is
  bob's id" — rather than leaving the reader to infer it.
- **Every clip carries a link to its whole source.** A committed capture, the
  cast, or a published board — something a reader can open and find the quoted
  lines inside.
- **Quote consecutively, and edit almost nothing.** Two changes are allowed
  and both are disclosed under the rail: agent ids shortened, long lines
  wrapped. No rewording, no reordering, no line that a run did not print, and
  no re-enactment of something that did not happen.
- **The motion is never the only way to read it.** The loop needs a second
  copy of the clips, and only the script makes one, so the drift is enabled by
  a class rather than by CSS. With the script blocked, or under
  `prefers-reduced-motion`, the rail is an ordinary horizontal scroller with
  every clip in it. It pauses on hover, on focus, on a scroll by hand, and on
  a checkbox that needs no script at all.
- **Never claim adoption we do not have — and do not announce its absence
  either.** The note under the rail used to end "we are not claiming outside
  users we do not have", which is a sentence that volunteers a weakness to a
  reader who had not asked and could not otherwise tell. Gal cut it, and the
  rule that replaces it is the useful half: the page may never state or imply
  outside use while there is none. It does not owe anyone an inventory of what
  it lacks. Every clip links to the run it came from, so whose run it was is
  one click away for anybody who wants to know.
- **Usage, not history.** What runs on it now, not what each run turned up.
  The defect inventories in `gald33/ai-lab` are worth reading and are not this
  page's argument.

---

## Open decisions (not principles — Gal's call)

1. ~~**Domain layout.**~~ **Decided.** `agentswitchboard.org` is the landing
   page and nothing else. The viewer stays on `gald33.github.io/switchboard`
   and the hub keeps its own hostname — three hosts on purpose, and the page
   says why (principle 9).
2. ~~**Hosting for the landing page.**~~ **Decided: Vercel**, serving `site/`
   from this repo with no build command
   ([`site/vercel.json`](vercel.json)).

   **Correcting the record.** An earlier revision of this document said
   "Decided: Cloudflare Pages". That was never decided — Cloudflare was named
   only for *nameservers*, and Pages was a recommendation of mine that got
   written down as a decision a couple of turns later. The house pattern is
   GitHub Pages for trusted static pages, otherwise Vercel.

   **GitHub Pages cannot take this one**, which is the whole reason Vercel
   wins: a repository gets exactly one Pages site, and this repository already
   spends it on the viewer at `gald33.github.io/switchboard`. Attaching the
   apex here would move the viewer onto it and break every invite that
   `switchboard invite --link` has already minted. Serving the page from a
   second repository would work and need no credentials at all, but it would
   separate `record-demo.py` from the `demo/run.sh` it records, so the cast
   could not be regenerated from one checkout.

   Vercel keeps the page beside the demo it records, needs no build, and needs
   no secret in this repository — its GitHub app holds the auth. Cloudflare
   stays what it always was here: DNS.

3. ~~**Does the GitHub Pages viewer move?**~~ **Decided: it stays**, and
   deliberately so — see principle 9. Moving it to a domain we control would
   trade away the one claim a visitor can check in thirty seconds.
