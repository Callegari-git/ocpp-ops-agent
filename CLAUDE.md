# CLAUDE.md

OCPP operations agent: an open-source simulator of an OCPP 1.6J charging-station fleet that injects realistic faults, plus an AI agent that diagnoses, verifies and fixes them under guardrails, benchmarked against a rule-based script.

Plan, current status and past decisions: @ROADMAP.md
Read the current week's section before starting any task.

## Commands

> Some of these don't exist yet. Create them when the roadmap reaches them.

- `pip install -e ".[dev]"`: install with dev dependencies (Python 3.11+)
- `make test`: run pytest
- `make lint`: ruff check + ruff format --check
- `make demo`: start the CSMS + fleet defined in `fleet.yaml`, show the live status table

## Stack

- Python 3.11+, asyncio, `ocpp` library (Mobility House) for both CSMS and chargers, `websockets`
- Pydantic for configs and data models, PyYAML for the fleet file
- pytest + pytest-asyncio, ruff, GitHub Actions
- Later: MCP Python SDK (agent tools, week 3), Docker (week 5)

## Layout

```
src/ocpp_ops/
  csms/       # minimal Central System: handlers + charger registry + outbound commands
  charger/    # virtual charge point: state machine, config store, OCPP handlers
  fleet/      # fleet file loading + launcher for N chargers
  faults/     # fault catalog + injection engine (week 2)
  agent/      # agent, MCP tools, guardrails, audit log (week 3)
  eval/       # agent vs rules harness + results (week 4)
tests/
fleet.yaml
logs/         # JSONL message logs (git-ignored)
```

## Hard rules

- **OCPP 1.6J only.** No OCPP 2.0.1 code paths until the roadmap says so.
- **Nothing from TotalEnergies.** No data, config, logs, code or internal procedures. Faults come from the OCPP 1.6 spec and general field knowledge only.
- **No code copied from GPL projects** (e.g. SteVe). Read them for ideas, write our own code. This repo is MIT.
- **Never run `git commit` or `git push`.** I commit and push myself. You may run read-only git commands (`git status`, `git diff`, `git log`) and suggest a commit message when a task is done.
- **No new dependencies without asking first.**
- **Stay in scope.** No web UI, no real database (in-memory or SQLite), no auth/TLS yet. See "Out of scope" in the roadmap.

## Design invariants

- **Every OCPP message is logged** as one JSONL line: timestamp, charger_id, direction, action, message_id, payload. This log is the agent's context and the audit trail.
- **Reproducibility:** all randomness goes through a seeded `random.Random` passed in explicitly. Never use the global `random` module. The same seed must produce the same run.
- **The charger state machine is explicit.** Status transitions go through one place, so faults can be injected there later.
- **Ground truth stays hidden from the agent.** The fault engine knows the cause and the fix. The agent only sees what a real operator would see (OCPP messages, config, status).
- **Agent guardrails (week 3+):** never `Reset` a connector with an active transaction; dry-run/simulate before applying; human approval for risky actions; every agent action goes into the audit log.

## Code style

- Type hints everywhere. Small modules and small functions.
- Simple and readable over clever: I need to be able to explain every line in an interview.
- Comment the *why* of anything non-obvious (OCPP quirks especially, with the spec section).
- Every new behavior gets a test. CI must stay green.

## Working with me

- Before coding a non-trivial task, give a short plan and wait for my OK.
- Explain non-obvious code you write, briefly.
- If a request conflicts with the roadmap or the rules above, say so instead of silently doing it.
- **At the end of each session:** tick completed boxes in `ROADMAP.md`, add any new decision to its "Decisions log" with the reason, and note open questions.
