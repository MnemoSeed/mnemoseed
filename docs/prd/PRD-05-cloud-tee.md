# PRD-05 · Cloud Sync, TEE Dreaming & Billing Gateway (MnemoSeed Cloud)

> Design docs: [04-isolation-and-privacy](../design/04-isolation-and-privacy.md), [08-sync-and-merge](../design/08-sync-and-merge.md) (sync protocol and conflict-merge semantics follow 08)
> Milestone: M4 (commercialization) · Estimate 30 days

## 1. Goals

Launch the $9/month MnemoSeed Cloud: physical isolation for 3 Profiles, multi-device E2EE sync, dreaming inside Nitro Enclaves, and a dynamic model-routing arbitrage gateway. Zero plaintext persists anywhere along the cloud path.

## 2. Scope

- **In**: E2EE sync protocol, multi-Profile isolation, Enclave dream execution environment + attestation, dynamic routing gateway (Sonnet / GPT-5.6 Terra), compute-coin billing
- **Out**: enterprise private clusters (License channel negotiated separately)

## 3. Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-5.1 | BYOK: client-side private-key derived encryption; keys never leave the device; the cloud only stores ciphertext blobs | P0 |
| FR-5.2 | Multi-device sync: ciphertext blobs + provenance-ordered replay, with offline queue recovery | P0 |
| FR-5.3 | Profile isolation: ≤3 independent Profiles per account, zero data leakage across Profiles | P0 |
| FR-5.4 | Enclave dreaming: memory is briefly decrypted only inside the Nitro Enclave; plaintext is destroyed as soon as reflection completes | P0 |
| FR-5.5 | Attestation verification interface: clients can cryptographically verify that the Enclave runs the official, untampered image | P0 |
| FR-5.6 | Dynamic routing: long-context deep reflection → Kimi K3 (Fireworks, cache read $0.30/M); short increments (dynamic budget ≤32k, PRD-02 FR-2.5) → DeepSeek V4 Flash 0731 (Fireworks, $0.14/M input) | P0 |
| FR-5.7 | Billing: hybrid of Profile count + brain-capacity compute coins; $5 = 1M compute points; free tier gets a monthly 500k incremental dream allowance (within the standard edition) | P0 |
| FR-5.8 | Cloud multi-user account system: email signup + Google sign-up (OAuth bound to the official domain); team invitations and seat management | P0 |
| FR-5.9 | Commercial License channel: self-hosted multi-user activation (Ed25519-signed license verified offline; entitlements: multi_user/seats/validity period); 30-day grace after expiry, after which multi-user logins are disabled but the owner and data remain intact (data is never touched); self-hosted deployments can configure their own Google OAuth client | P0 |
| FR-5.10 | Admin Plane super-admin interface: service health / growth & sales (signups/funnel/MRR/license activation) / user operations (quotas/bans) / billing costs (model-routing breakdown/gross margin/TEE utilization); **red line: operations metadata only — memory plaintext is physically invisible (guaranteed by the BYOK architecture, not by discipline)**; super-admin uses independent strong authentication (TOTP/hardware keys), and all operations go into an immutable audit log | P0 |

## 4. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-5.1 | Only official enterprise channels (AWS Bedrock / Vertex AI) + ZDR endpoints; OpenRouter-class intermediaries are prohibited |
| NFR-5.2 | Single TEE physical cost ≤ $80/month, supporting ≥ 200 paying users (gross margin ≥ 60%) |
| NFR-5.3 | Compliance: GDPR / CCPA / PDPA data-processing agreements ready |
| NFR-5.4 | Sync conflict-merge semantics follow design/08: append-only data converges via CRDTs (chunks = G-Set, score pool = PN-Counter, graph = content-hash); real contradictions are not arbitrarily sided with and go through Reconcile flag_conflict for explicitness; deletion uses the Tombstone OR-Set |

## 5. Acceptance Criteria

- AC-1: Pull the plug on the cloud database and audit it directly — zero plaintext;
- AC-2: Two devices converse alternately; memories converge consistently and history chains stay complete;
- AC-3: A third-party security researcher verifies the Enclave image hash via the attestation interface;
- AC-4: Billing sandbox: 100 heavy users' monthly model-API cost ≤ 40% of compute-coin revenue.

## 6. Task Breakdown

1. `cloud/sync` — E2EE sync protocol + offline queue (6d)
2. `cloud/enclave` — Nitro Enclave image + attestation (8d)
3. `cloud/router` — dynamic routing gateway + Prompt Cache management (5d)
4. `cloud/billing` — compute-coin ledger + Stripe integration (5d)
5. Security audit + compliance documentation (6d)

## 7. Dependencies

- M1–M3 fully complete (local closed loop validated, community trust assets in place)
