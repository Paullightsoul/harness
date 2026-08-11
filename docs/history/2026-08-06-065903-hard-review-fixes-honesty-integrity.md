---
date: 2026-08-06T06:59:03+00:00
source: dialog
project: 1.harness-v4
---

# Hard-review fixes: honesty default ON + anti-forge + suite green

## Решения
- V4 default `HARNESS_EVIDENCE_GATE=1` with escape `=0` (honesty effective out of the box).
- Real forge-resistance via HMAC attestation + `acceptance.seal` + trusted evidence sources; CLI green-gate mint refused unless `HARNESS_EVIDENCE_CLI_MINT=1`.
- Lease acquire/release under `flock`; legacy Engine lessons only to `HARNESS_BRAIN_ROOT` (never `/home/brain`).
- Zy dogfood regressions moved to fixtures (live PLAN drift is not a control-plane failure).

## Что сделано
- `harness/evidence/integrity.py` + wired into ledger/acceptance/`done_allowed`.
- Lease flock; Engine/brain-agents harden; safety tests on `.harness/runs/`.
- Docs: HONESTY-MODE, GROK-DEFAULTS, .env.example; soft-gates inventory refresh in ZY reports.
- Pytest: 8 fails → **682 passed, 2 skipped**.

## Итерации
- Anti-forge test initially forged already-green acceptance (no-op); rewritten to flip without seal.
- PLAN fixture needed `depends_on` column header for parser.

## Диалог
User asked to fix all Critical/Major findings from the v4 hard review in `/home/1.harness-v4` only (no prod tree, no human brain writes). Shipped integrity + defaults + suite green; deferred live dogfood and shared-PLAN lease design with reasons in the fixes report.
