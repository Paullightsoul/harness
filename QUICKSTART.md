# Quickstart — TaskTool-first harness

Full guide → `GUIDE.md`. Architecture → `ARCHITECTURE.md`.

---

## A. One-time setup

```bash
cd /home/1.harness          # this repo (branch agents/tasktool-harness-v3)
make setup                  # venv + deps + .env + doctor
. .venv/bin/activate
```

Edit `.env` for TaskTool routing (see `.env.example`):

```ini
TASKTOOL_ORCH_MODEL=cursor-grok-4.5-high
TASKTOOL_WORKER_MODEL=cursor-grok-4.5-high
TASKTOOL_REVIEWER_MODEL=cursor-grok-4.5-high
```

Grok-first defaults — see `docs/GROK-DEFAULTS.md`.

`CURSOR_API_KEY` is only required for legacy SDK/`harness run` paths.

---

## B. Register a target project

```bash
harness init --repo /abs/path/to/your-repo
harness projects add --name myapp --repo /abs/path/to/your-repo --base main
harness doctor --project myapp
```

Never point `--repo` at `/home` (meta-root guard).

---

## C. In a new Cursor chat (primary UX)

1. Attach text/docs/specs.
2. Say: **«используй harness»** (or «use harness»).
3. Root chat follows `.cursor/skills/harness-orchestrate/SKILL.md`:
   - `harness context --project myapp` → project context для планнера
   - planner Task writes `PLAN.md` + `tasks/*`
   - `harness verify`
   - plan approval (`AskQuestion`)
   - `harness tasktool start … --approve-plan --spec-source …`
   - loop: `next` (max 8) → Task Tool → `report` → `advance`
     (large-задачи проходят sub-orchestrator декомпозицию автоматически)
   - risk / ship approvals when required
   - `resume` after chat restart

You should **not** start a long-lived Python runner or a file-spool poller.

---

## D. Manual CLI skeleton (same contract)

```bash
harness verify --project myapp
harness tasktool start "Добавить REST API пользователей" \
  --project myapp --repo /abs/path/to/your-repo --base-branch main \
  --approve-plan --spec-source ./spec.md

RUN=$(harness tasktool status …)   # keep run_id from start JSON

harness tasktool next "$RUN" --project myapp --repo /abs/path/to/your-repo \
  --agent-id root-chat --limit 12
# write result JSON to the envelope's result_path, then:
# --agent-id holds the lease (root chat); --worker-id identifies the executor and
# must be distinct per worker, or the anti-cosplay check refuses DONE.
harness tasktool report "$DISPATCH" --project myapp --repo /abs/path/to/your-repo \
  --result-file /path/to/result.json --agent-id root-chat \
  --worker-id "worker-$DISPATCH" --ok
harness tasktool advance "$RUN" --project myapp --repo /abs/path/to/your-repo
```

Minimal result file:

```json
{"dispatch_id":"dispatch-1","final_text":"HARNESS_DONE"}
```

Ship only after explicit approval:

```bash
harness tasktool advance "$RUN" --project myapp --repo /abs/path/to/your-repo \
  --approved-merge
```

---

## E. Chat restart

```bash
harness tasktool status "<run-id>" --project myapp --repo /abs/path/to/your-repo
harness tasktool resume "<run-id>" --project myapp --repo /abs/path/to/your-repo
```

Если run ждёт ответа или отдельного risk approval:

```bash
harness tasktool resume "<run-id>" --project myapp --repo /abs/path/to/your-repo \
  --answer q1="ответ"
harness tasktool resume "<run-id>" --project myapp --repo /abs/path/to/your-repo \
  --approve-risk
```

Do not create a second active run for the same repo/goal.

---

## F. Legacy (deprecated compatibility)

`harness mode chat` / file-spool bridge and `harness plan`→`ingest`→`run` still
exist. Prefer TaskTool pull mode for all new work. Background unattended workers
are **not** default — see `BACKLOG-background-mode.md`.
