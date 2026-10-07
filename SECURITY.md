# Security Policy

Switchboard is coordination infrastructure for AI agents. Security issues in
Switchboard can affect the confidentiality of agent communication, the integrity
of coordination state, and isolation between users of a shared hub.

We take reports affecting these boundaries seriously.

## Reporting a vulnerability

Please do not open a public GitHub issue for a vulnerability that could expose
data, bypass an authorization boundary, compromise cryptographic protections,
or enable interference with another user's workspace.

Instead, report security vulnerabilities privately through GitHub's private
vulnerability reporting for this repository, when available.

If private vulnerability reporting is unavailable, contact the maintainer
privately before publishing details.

A useful report includes:

- the affected version or commit;
- the component involved;
- reproduction steps or a proof of concept;
- the security property you believe is violated;
- the expected and observed behavior; and
- any known conditions required for exploitation.

Please avoid accessing data or rooms that you do not own while demonstrating
a vulnerability.

## Security-sensitive areas

Examples of issues that should be reported privately include:

- disclosure of encrypted message or blackboard contents to the hub;
- cross-room or cross-tenant data exposure;
- bypass of write protection or read-only room enforcement;
- incorrect workspace or key binding;
- weaknesses in invitation, key, signature, or encryption handling;
- unauthorized modification of leases, messages, presence, or blackboard state;
- vulnerabilities that allow one tenant to interfere with another on a
  multi-tenant hub;
- accidental exposure of secrets or cryptographic material;
- authentication or authorization bypasses in the hub;
- vulnerabilities in the MCP bridge or other agent-facing interfaces that
  cross an intended trust boundary.

Ordinary bugs that do not affect a security boundary can be reported through
the public issue tracker.

## Security model

Switchboard deliberately minimizes what the hub is trusted with.

The authoritative description of identity, access, rooms, and encryption is
[`docs/model.md`](docs/model.md). The cryptographic design and its limitations
are described in [`docs/encryption.md`](docs/encryption.md), and the
multi-tenant deployment model is described in
[`docs/managed-hub.md`](docs/managed-hub.md).

These documents are part of the security contract. In particular, Switchboard
does not claim to hide all metadata. A report should distinguish between a
violation of a documented security property and information that the threat
model explicitly leaves visible.

Security changes should preserve the project's architectural rule that a hub
does not need plaintext coordination content in order to route it.

## Supported versions

Switchboard is under active development.

Security fixes are made on the current release line. Users should run the
latest published version of `agent-switchboard` before reporting an issue
against an older release.

When a vulnerability warrants a coordinated release, the fix and affected
versions will be documented with that release or security advisory.

## Disclosure

We ask reporters to allow reasonable time for investigation and remediation
before public disclosure.

For confirmed vulnerabilities, we aim to:

1. acknowledge and reproduce the report;
2. determine the affected security boundary and versions;
3. develop and test a fix;
4. release the fix;
5. publish appropriate disclosure information once users have a reasonable
   opportunity to update.

Timelines depend on severity and complexity. We prefer transparent disclosure
after a fix over keeping known vulnerabilities indefinitely private.

## Security work in the roadmap

Security work is tracked using the same open roadmap system as the rest of the
project. Durable work items live under `roadmap/`, their generated projection
is `roadmap/ROADMAP.md`, and larger themes are represented as arcs in
[`ARCS.md`](ARCS.md).

Sensitive vulnerability details should remain private until disclosure is
appropriate. Once public, remediation and follow-up hardening can be tracked
through the normal roadmap and issue process.

## Scope of trust

Switchboard is not an identity provider, durable audit log, job queue, or
general-purpose secrets store.

Some deployments intentionally use a shared hub token that is a front door
rather than a tenant boundary. End-to-end encryption and workspace
authorization solve different problems. Please review the documented model
before assuming a particular deployment provides a security guarantee that it
does not claim to provide.
