# Threat Model

## Scope

The threat model covers the *platform*: control plane, runtime, services,
and shared substrate. It does **not** cover the legal environment of the
deployer.

## Assets

1. **Automaton identities** (signing keys).
2. **Treasury balances** (real money).
3. **Treasury ledger** (history).
4. **Memory** (private thoughts, plans, financial memory).
5. **Audit log** (chain integrity).
6. **Plugins** (third-party code).
7. **Sandboxes** (host kernel, container runtimes).
8. **Marketplace** (reputation, settlement).

## Adversaries

| Class                  | Capabilities                                                            |
|------------------------|-------------------------------------------------------------------------|
| Curious user           | Read APIs only; tries to enumerate or scrape.                           |
| Malicious user         | Authenticated; tries to fund or drain other Automata.                   |
| Compromised plugin     | RCE in a sandbox; tries to escape or pivot.                             |
| Compromised worker     | Full runtime inside the host; tries to access other Automata.           |
| Compromised DB         | Can read encrypted-at-rest rows; tries to forge or replay.              |
| Compromised cloud      | Can read storage, snapshots, audit; tries to forge history.             |
| Nation-state           | All of the above + supply chain.                                        |

## Threats and mitigations

| Threat                                 | Mitigation                                                          |
|----------------------------------------|---------------------------------------------------------------------|
| Treasury drain by runaway loop         | BudgetController + per-tick / per-day caps; alerts                  |
| Unauthorized data exfiltration         | Default-deny network in sandboxes; egress allowlists                 |
| Tool supply-chain compromise            | Signed plugin artifacts; SBOMs; reproducible builds                  |
| Sandbox escape                          | microVM by default for high-risk tools; seccomp + cgroups            |
| Audit log tampering                     | Hash-chained rows; signed by audit service key                       |
| Key compromise                          | Ed25519 key rotation; history with self-signatures                  |
| Replay of old signed actions            | Nonces + sequence numbers in signed envelopes                       |
| Identity spoofing                       | Per-tenant signing keys; cross-sign on funding transactions         |
| LLM prompt injection                    | Constitution + structured output validation; per-tool I/O filtering  |
| Insider abuse                           | Per-user audit + 4-eyes approval on critical ops                     |
| DoS via expensive queries               | Per-tenant rate limits; query budgets                                |
| Secret leak                             | Secret backend; no env-var secrets in production                    |
| Replicated child liability              | Independent keypair; audit log records the parent but child acts autonomously |

## Residual risk

- A determined nation-state that compromises the cloud account can
  rewrite history *if* it also compromises the audit service's signing
  key. We mitigate with HSM-backed key storage and external notarization
  (configurable).
- A 0-day in the microVM kernel escapes the sandbox. We mitigate by
  always running tools as a non-root user, by default-deny egress, and
  by rate limiting.

## Security boundaries

- **Process boundary**: each tool runs in its own subprocess with
  rlimits. *Not* a security boundary against the kernel.
- **Container boundary**: OCI image, read-only root, no network by
  default. Reasonable isolation.
- **MicroVM boundary**: Firecracker. Strongest available; used for any
  high-risk tool.
