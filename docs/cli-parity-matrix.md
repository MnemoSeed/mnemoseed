# MnemoSeed CLI Parity Matrix

Every console action is scriptable through the `mnemoseed` CLI (PRD-07 FR-7.12,
G-AC5; design/07 §5). The matrix is the acceptance source of truth: a
parser-registration test (`tests/test_cli_parity.py`) reads every
``mnemoseed <verb>`` token below and asserts the verb is registered, and the
audit actor attribution column is what the daemon records for each write.

Legend: actor ∈ `cli` | `console` | `mcp` · transport REST = daemon REST API
(loopback or token-auth) · offline = direct config/installer access, never the
daemon REST.

| Console surface | CLI verb | State transition | Daemon REST endpoint | Audit actor |
|---|---|---|---|---|
| ① Dashboard | `mnemoseed status` | read-only | `GET /api/v1/status` | — |
| Console launcher | `mnemoseed console` | opens `{baseurl}/console` | static SPA | — |
| Memory browse (chunks/nodes) | `mnemoseed recall "<query>"` | read-only | `POST /memory/recall` | — |
| ② Memory write / pin | `mnemoseed remember "<fact>"` | upsert chunk / reinforce | `POST /memory/remember` | cli |
| ③ Dream panel | `mnemoseed dream --once` | run one manual dream cycle | `POST /api/v1/dream/once` | cli |
| ③ Dream panel (status) | `mnemoseed dream status` | read-only | `GET /api/v1/dream/status` | — |
| ⑧ Models page | `mnemoseed llm set <role> --driver/--model/--base-url/--api-key-env` | persist one role-route (validated, audited) | `POST /api/v1/llm/routes/{role}` | cli |
| ⑧ Models page | `mnemoseed llm status` | read-only route table + connectivity probe | offline (config file) | — |
| Export | `mnemoseed export` | read-only dump | `POST /memory/export` | — |
| Memory dossier (versions) | `mnemoseed diff <node_id>` | read-only | `POST /memory/audit` | — |
| ④ Memory browser (delete) | `mnemoseed forget <target> [--kind node|chunk|entity]` | tombstone / delete | `POST /memory/forget_this` | cli |
| ② Memory write / pin | `mnemoseed pin <node_id> [--off]` | flip a node's never_decay (version-chain append) | `POST /api/v1/pin` | cli |
| ④ Memory browser (weights) | `mnemoseed weight <target> <0..1> [--kind node|chunk]` | adjust decay_weight (bounded [0,1]) | `POST /api/v1/weights` | cli |
| ⑥ Conflicts inbox | `mnemoseed conflicts list` | read-only inbox | `GET /api/v1/conflicts` | — |
| ⑥ Conflicts inbox | `mnemoseed conflicts resolve <id> --branch reinforce|coexist|invalidate|pending [--node <id>] [--cues <scope>]` | resolve one conflict group (reinforce / coexist / invalidate / pending) | `POST /api/v1/conflicts/{group_id}/resolve` | cli |
| ⑤ Admin audit | `mnemoseed audit [--actor] [--action]` | read-only | `GET /api/v1/audit` | — |
| Settings | `mnemoseed config get [key]` | read-only | `GET /api/v1/config` | — |
| Settings | `mnemoseed config set <key_path> <value>` | versioned config write | `POST /api/v1/config/set` | cli |
| Settings | `mnemoseed config versions` | read-only history | `GET /api/v1/config/versions` | — |
| Settings | `mnemoseed config rollback <version_id>` | revert to prior config version | `POST /api/v1/config/rollback` | cli |
| Host binding | `mnemoseed link` | write profile_id + token env into each host config | offline (installer, backup+diff+confirm) | — |
| Host binding | `mnemoseed unlink` | remove mnemoseed entry from each host config | offline (installer) | — |
| Onboard wizard | `mnemoseed onboard [--skip llm] [--resume]` | guided aggregate over the shared onboard backend | `/api/v1/setup` + config + llm routes | cli |

Notes:

- **Identical state transitions**: CLI and console route through the same daemon
  REST surface; the console never implements its own onboarding flow and the
  CLI never writes config.toml except the `config set --force` offline escape
  (PRD-07 FR-7.12, design/06 §6).
- **Loopback rule**: `config` operations are loopback-only — against a
  non-loopback baseurl they fail with a clear error rather than mutating a
  remote instance's config; `--force` prints "not audited (daemon down)".
- **Actor attribution**: every state-changing CLI verb forwards
  `X-MnemoSeed-Actor: cli`; the daemon records who changed what (design/07 §5).
- **`--json`**: every verb with a table rendering also accepts `--json` for
  machine-readable output (G-AC5).
