# Honesty mode (Phase 1.5 + semantic bind)

**V4 default: ON.** DONE requires sealed acceptance + HMAC-attested ledger
evidence with **1:1 semantic AC↔evidence binding**. Emergency escape hatch only:

```bash
export HARNESS_EVIDENCE_GATE=0   # solo/debug only — honesty inactive
```

## Enable (default)

```bash
export HARNESS_HOME=/home/1.harness-v4
cd /home/1.harness-v4 && . .venv/bin/activate

# Primary honesty switch (default 1 in v4):
export HARNESS_EVIDENCE_GATE=1   # optional — already the default

# Soft/optional sensors stay refused unless you opt in (default 0):
export HARNESS_ALLOW_SOFT_GATES=0

# Semantic bind (default ON) — blocks news-bot mass-flip class:
export HARNESS_EVIDENCE_EXCLUSIVE_BIND=1
export HARNESS_REQUIRE_DECLARED_BINDING=1
# Escape hatches (solo/debug only):
# export HARNESS_ALLOW_EVIDENCE_REUSE=1
# export HARNESS_ALLOW_MASS_FLIP=1

# Worker boundary / anti-cosplay (default ON for medium+):
export HARNESS_REQUIRE_NESTED_WORKERS=1
# Hard refuse DONE when cosplay detected (default ON):
export HARNESS_NESTED_WORKERS_HARD_FAIL=1
# Escape hatch (emergency warn-only):
# export HARNESS_NESTED_WORKERS_HARD_FAIL=0
```

With `HARNESS_EVIDENCE_GATE=1` (default):

1. Gates append **attested** rows to `.harness/runs/<id>/evidence/ledger.jsonl`
   (HMAC via control-plane secret; a row edited on disk no longer verifies —
   see «Scope of the guarantee» below for what this does and does not cover).
2. `acceptance.json` items flip to `passes:true` **only** via control plane
   (`harness acceptance flip` or auto-flip of `ac-gate-*` after green gates).
   Each write seals `acceptance.seal`; DONE refuses a mismatched/missing seal.
3. **Semantic bind:** each evidence_id may attach to **at most one** AC item.
   Ledger rows with `acceptance_item_ids` may only flip those ids (gate rows
   auto-declare `ac-gate-<gate_id>`). Reusing one suite gate blob across ten
   functional ACs is **refused**.
4. `advance` / manual ship / merge → DONE is refused unless `done_allowed()`.
   Reviewer / goal-judge `VERDICT: APPROVE` is a **no-op** when evidence is red
   (`judge_approve_blocked`) — judges cannot flip acceptance.
5. Status shows **evidence%** primary (+ **ac_bound%**); pipeline/FSM% is secondary.
6. CLI `harness evidence add --exit-code 0` for `kind=gate` is **refused**
   unless `HARNESS_EVIDENCE_CLI_MINT=1` (operator override). Green gate evidence
   must come from ProfileGate / lifecycle `record_gate_run`.
7. Flips append an audit ledger row (`kind=acceptance_flip`).

See also Phase 2 governors: `docs/PHASE2-GOVERNORS.md`.
Hard-analysis fix write-up: `HARNESS-V4-HONESTY-SEMANTIC-FIX-2026-08-06.md`
(in ZY `harness_reports/`).

## evidence% formula (honesty)

| Metric | Meaning |
|---|---|
| **evidence%** | Share of required (non-waived) items that are `passes:true` **and** have valid **exclusive** attested evidence binding |
| **ac_bound%** | Share of required items with valid exclusive attested binding (passes optional) |
| **passes%** | Raw `passes:true` share (diagnostic only — can be inflated by reuse) |

Unbound `passes:true` (shared evidence_ids / undeclared gate blob) does **not**
inflate evidence%. YouTrack readiness still maps to **evidence_pct**.

## Integrity (anti-forge)

| Artifact | Protection |
|---|---|
| `evidence/ledger.jsonl` | Each row carries `attestation.sig` = HMAC-SHA256 of the row (secret from `HARNESS_EVIDENCE_SECRET` or `$HARNESS_HOME/.harness/secrets/evidence.hmac`) |
| `acceptance.json` | Companion `acceptance.seal` (content sha256 + HMAC); rewritten only by control-plane save/flip/waive |
| Trusted sources | `advance`, `profile_gate`, `gate_runner`, `control_plane`, `waive:*` — not bare `cli` |
| Semantic bind | 1:1 evidence↔AC; declared `acceptance_item_ids`; mass-flip refused |

Hand-editing run files to force DONE **fails** `done_allowed()` without the HMAC secret.
Sharing one evidence id across many ACs **fails** `done_allowed()` even with a valid seal.

### Scope of the guarantee — read this before relying on it

Attestation is **tamper-evidence, not a privilege boundary.** Task Tool agents run
as the same OS user as the control plane, which means:

- the key file is readable by the agents it defends against — an agent that goes
  looking can re-sign anything;
- `HARNESS_EVIDENCE_CLI_MINT=1` is an ordinary environment variable, so an agent
  can set it for a command it runs and mint green gate rows via `harness evidence add`.

What it does buy: an agent that edits `acceptance.json` or `ledger.jsonl` to look
finished — the realistic failure mode — is caught at the gate, and every green row
is attributable. Making it an actual boundary requires privilege separation
(separate uid, container, or a signing service the agent cannot call); the current
single-user host provides none of those.

`harness doctor` prints the live posture (key source, permissions, whether CLI mint
is open) so this stays visible; `attestation_posture()` returns the same data.

## Worker boundary (anti-cosplay)

Reports record `worker_id` / `agent_kind` / `agent_id`. Status sets
`cosplay_risk=true` when medium+ implement tasks were all reported by the same
agent id (orch==worker cosplay) or nested worker ids are missing. **Default ON:**
`HARNESS_NESTED_WORKERS_HARD_FAIL=1` refuses DONE/advance on cosplay. Emergency
escape hatch (detect+flag only): `HARNESS_NESTED_WORKERS_HARD_FAIL=0`.

Root chat should **dispatch only** — spawn nested Cursor Task workers from
`next` envelopes with distinct agent/worker ids.

## Resume / envelope waste

When DONE/APPROVE is blocked on acceptance, the run enters
`stall_reason=acceptance_repair:…`. Resume does **not** refill implement
workers until acceptance is green (or the flag is cleared). `_enqueue` also
refuses duplicate live PENDING/CLAIMED/RUNNING envelopes of the same kind.

## CLI

```bash
harness evidence list --run-id <id> --repo /path/to/target
# Green gate mint via CLI is refused by default:
# harness evidence add … --exit-code 0   → error unless HARNESS_EVIDENCE_CLI_MINT=1

harness acceptance show --run-id <id> --repo …
# → evidence_pct + ac_bound_pct + passes_pct
harness acceptance flip --run-id <id> --repo … --item ac-001 --evidence ev-…
harness acceptance waive --run-id <id> --repo … --item ac-001 --reason "…"

harness status --markdown   # evidence% + ac_bound% first
```

## Soft gates

ZY / PHP scaffold: `phpunit` is **blocking**. Missing vendor → exit 1.
Legacy `phpunit-optional` / `|| exit 0` swallow patterns are hardened at runtime
by `ProfileGate` when `HARNESS_ALLOW_SOFT_GATES=0`.

## YouTrack

If `.env.local.youtrack` / `YOUTRACK_TOKEN` is missing, `youtrack_readiness`
returns `available=false` with a ready `marker_block` — do not publish FSM%.
