# Governance

Switchboard is an open-source project currently led and maintained by
Dr. Gal Danino (`@gald33`).

The project is early enough that it does not need a steering committee or
formal voting process. It does, however, maintain an explicit and public
system for deciding, tracking, implementing, and reviewing work.

This document describes that system and how responsibility can evolve as the
community grows.

## Maintainer

Dr. Gal Danino is currently the lead maintainer and final decision-maker for
the project.

The lead maintainer is responsible for:

- the project's architectural direction;
- accepting and merging contributions;
- releases;
- security-sensitive decisions;
- maintaining the roadmap and its priorities; and
- resolving design questions that do not reach consensus.

This is intentionally simple governance for the project's current size. It is
not intended to prevent responsibility from being delegated as sustained
contributors emerge.

## How work is proposed

Bugs, feature requests, and design questions may begin as GitHub issues.

Substantial work should be represented in the project's roadmap before or as
it is implemented.

The roadmap is itself an open-source, file-backed system:

- `roadmap/items/*.yaml` contains durable work items;
- `roadmap/arcs/*.yaml` groups related work into larger themes;
- `roadmap/ROADMAP.md` is the generated backlog projection;
- [`ARCS.md`](ARCS.md) is the generated narrative view explaining why larger
  themes remain open.

GitHub issues hold discussion. The roadmap records what the project intends to
do about that discussion and in what order.

The source YAML, rather than the generated Markdown projections, is the source
of truth.

## Prioritization and claiming work

The roadmap records dependencies, status, and priority.

Contributors and agents can inspect work that is ready to begin with:

    roadmap ready

and claim a work item before starting it with:

    roadmap claim <key>

When a tracked item is completed, its status should be updated in the same
change:

    roadmap status <key> done
    roadmap sync

CI verifies that generated roadmap views remain synchronized with their source
files.

A roadmap claim represents durable intent to work on an item. It is distinct
from Switchboard's ephemeral coordination leases. The distinction is
intentional: project intent belongs in durable project state; temporary
coordination should expire.

## Design decisions

Switchboard has a small set of deliberate architectural constraints documented
in the repository.

Contributors should preserve those constraints unless a change explicitly
proposes revisiting them.

In particular, proposals that alter the fundamental coordination model,
security model, or trust boundaries should begin with discussion rather than
implementation.

For example, the project's four coordination primitives — presence, leases,
messages, and blackboard — are deliberately kept small. A proposal for a new
primitive should first demonstrate why it cannot be expressed cleanly using
the existing model.

The authoritative security and identity model is documented in
[`docs/model.md`](docs/model.md).

Architectural decisions should be recorded in durable repository artifacts,
not left only in conversations.

## Pull requests

Pull requests should follow [`CONTRIBUTING.md`](CONTRIBUTING.md).

Behavioral changes should include tests. Changes to public interfaces should
update their corresponding documentation. Changes completing roadmap work
should update the roadmap in the same pull request.

The lead maintainer currently has final merge authority.

Security-sensitive changes may receive additional scrutiny even when they are
otherwise small.

## Becoming a maintainer

There is currently one maintainer.

As the project grows, maintainer responsibility may be delegated to
contributors who demonstrate:

- sustained, constructive contribution;
- understanding of Switchboard's architecture and security model;
- care for backwards compatibility and users;
- consistent review quality;
- responsible handling of security-sensitive work; and
- willingness to maintain work after it is merged.

Maintainer status is a responsibility, not a reward for contribution volume.

Future maintainers may be given responsibility for particular areas before
receiving project-wide merge authority.

When the number of active maintainers makes unilateral governance
inappropriate, this document should be revised to define the decision process
used by that group rather than pretending such a process exists today.

## Security

Security vulnerabilities follow [`SECURITY.md`](SECURITY.md), not the normal
public issue process while disclosure would put users at risk.

Once disclosure is safe, follow-up security work should become visible through
the normal issue and roadmap system whenever possible.

Changes affecting encryption, workspace isolation, authorization, signing,
key handling, or other documented trust boundaries require particular care.

## Releases

Releases are currently performed by the lead maintainer.

The SDK and independently distributed add-ons have separate release processes.
Release mechanics and ordering are documented in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Changes to governance

Governance should evolve with the project rather than anticipate an
organization that does not yet exist.

Changes to this document are made through the normal pull-request process.
Material governance changes should be discussed publicly before merge.

The goal is simple: keep decision-making transparent, keep the project's
durable intent in the repository, and add process only when the size of the
community requires it.
