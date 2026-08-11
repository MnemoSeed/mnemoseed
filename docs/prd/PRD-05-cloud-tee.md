# PRD-05 · Hosted Cloud Daemon & Billing (MnemoSeed Cloud)

> Design docs: [04-isolation-and-privacy](../design/04-isolation-and-privacy.md), [08-sync-and-merge](../design/08-sync-and-merge.md) (sync protocol and conflict-merge semantics follow 08)
> Milestone: M4 (commercialization) · Estimate 30 days

## 1. Goals

Launch MnemoSeed Cloud: the daemon hosted by us (SaaS), **running inside a TEE (Nitro Enclave) as standard** — hosts install only thin tools (MCP/hooks); features identical to self-hosted, differing only in account/profile limits. E2EE transport + encrypted at-rest storage are built into the app regardless of environment (design/04 §3); dream LLM egress goes only to ZDR APIs.

## 2. Scope

- **In**: hosted multi-tenant daemon (TEE as standard, tenant isolation), multi-device sync, dynamic routing gateway, usage-based billing
- **Out**: enterprise private clusters (License channel negotiated separately)

## 3. Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-5.1 | Encrypted storage & transport: the daemon persists ciphertext at rest; host tools ↔ daemon is E2EE with token auth; keys are derived and managed daemon-side, user-held when self-hosted | P0 |
| FR-5.2 | Multi-device sync: ciphertext blobs + provenance-ordered replay, with offline queue recovery | P0 |
| FR-5.3 | Profile isolation: ≤3 independent Profiles per account, zero data leakage across Profiles | P0 |
| FR-5.4 | TEE execution (SaaS standard): the daemon runs inside a Nitro Enclave; memory plaintext exists only briefly inside the enclave; no such requirement for self-hosted (users arrange their own environment; E2EE/encrypted storage unaffected) | P0 |
| FR-5.5 | Attestation verification interface (SaaS TEE): clients can cryptographically verify that the Enclave runs the official, untampered image | P0 |
| FR-5.6 | Dynamic routing: long-context deep reflection → Kimi K3 (Fireworks, cache read $0.30/M); short increments (dynamic budget ≤32k, PRD-02 FR-2.5) → DeepSeek V4 Flash 0731 (Fireworks, $0.14/M input) | P0 |
| FR-5.7 | Billing: hybrid model of Profile count + usage-based compute credits; specific pricing is a business decision tracked in internal docs | P0 |
| FR-5.8 | Cloud multi-user account system: email signup + Google sign-up (OAuth bound to the official domain); team invitations and seat management | P0 |
| FR-5.9 | Commercial License channel: self-hosted multi-user activation (Ed25519-signed license verified offline; entitlements: multi_user/seats/validity period); 30-day grace after expiry, after which multi-user logins are disabled but the owner and data remain intact (data is never touched); self-hosted deployments can configure their own Google OAuth client | P0 |
| FR-5.10 | Admin Plane super-admin interface: service health / growth operations (signups/funnel/license activation) / user operations (quotas/bans) / cost observability (model-routing breakdown/TEE utilization); **red line: operations metadata only — memory plaintext is physically invisible on our SaaS (TEE hardware-guaranteed); self-hosted users hold their own keys**; super-admin uses independent strong authentication (TOTP/hardware keys), and all operations go into an immutable audit log | P0 |

## 4. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-5.1 | Only official enterprise channels (AWS Bedrock / Vertex AI) + ZDR endpoints; OpenRouter-class intermediaries are prohibited |
| NFR-5.2 | TEE cost-to-user-capacity ratio must support a positive margin (specific targets are a business decision, tracked internally) |
| NFR-5.3 | Compliance: GDPR / CCPA / PDPA data-processing agreements ready |
| NFR-5.4 | Sync conflict-merge semantics follow design/08: append-only data converges via CRDTs (chunks = G-Set, score pool = PN-Counter, graph = content-hash); real contradictions are not arbitrarily sided with and go through Reconcile flag_conflict for explicitness; deletion uses the Tombstone OR-Set |

## 5. Acceptance Criteria

- AC-1: Pull the plug on the cloud database and audit it directly — zero plaintext;
- AC-2: Two devices converse alternately; memories converge consistently and history chains stay complete;
- AC-3: A third-party security researcher verifies the Enclave image hash via the attestation interface;
- AC-4: Billing sandbox: heavy-user model-API spend stays well below revenue (threshold lands with pricing).

## 6. Task Breakdown

1. `cloud/sync` — E2EE sync protocol + offline queue (6d)
2. `cloud/enclave` — Nitro Enclave image + attestation (8d)
3. `cloud/router` — dynamic routing gateway + Prompt Cache management (5d)
4. `cloud/billing` — usage ledger + payment integration (5d)
5. Security audit + compliance documentation (6d)

## 7. Dependencies

- M1–M3 fully complete (local closed loop validated, community trust assets in place)
