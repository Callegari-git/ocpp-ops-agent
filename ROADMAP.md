# Roadmap: OCPP Operations Agent

## Goal

Show, with public code and measured numbers, that an AI agent can run real charging-network operations (diagnose, verify, fix) safely, and show where it beats a rule-based script and where it doesn't.

- **Budget:** 5 weeks, ~15–20 h/week (80–100 h total)
- **Target:** public v1 (repo + demo video + results + article) before end of January 2027
- **Started:** 2026-11-24

## Success metrics (to publish)

- Resolution rate per fault type, agent vs rules
- Number of dangerous actions blocked by guardrails
- Mean time to resolution and cost per incident
- False positives: fixes applied when there was no fault

---

## Week 1: Simulator (CSMS + virtual fleet)

**Done when:** `make demo` starts the CSMS + 50 virtual chargers, and the terminal shows a table of 50 connected chargers with their status and last heartbeat.

### Setup

- [x] Run the Mobility House OCPP 1.6 central system + charge point examples locally and watch BootNotification / Heartbeat flow
- [x] Repo skeleton: `pyproject.toml`, `src/` layout, ruff, pytest, pytest-asyncio
- [ ] GitHub Actions CI (lint + tests) running on every push (workflow written; tick after the first green run)
- [x] Minimal README, including the "no TotalEnergies data, config or code" statement
- [x] `.gitignore` (incl. `logs/`), MIT license

### CSMS

- [ ] Handlers: BootNotification, Heartbeat, StatusNotification, MeterValues
- [ ] In-memory charger registry: status per connector, last heartbeat, known config
- [ ] Outbound commands: GetConfiguration, ChangeConfiguration, Reset (future agent tools)
- [ ] JSONL logging of every message in and out

### Virtual charger

- [ ] Explicit state machine (Available, Preparing, Charging, SuspendedEV, SuspendedEVSE, Finishing, Unavailable, Faulted), single transition point
- [ ] Config store with standard 1.6 keys (HeartbeatInterval, MeterValueSampleInterval, ConnectionTimeOut, ...)
- [ ] Boot → heartbeat loop → status notifications on transitions
- [ ] Respond correctly to GetConfiguration, ChangeConfiguration, Reset

### Fleet

- [ ] `fleet.yaml` format (charger templates: vendor, model, connectors, heartbeat interval; count per template)
- [ ] Launcher: N chargers in one asyncio process, seeded start-up jitter
- [ ] Live terminal status table

### Tests

- [ ] Integration: CSMS + 3 chargers → boot accepted, heartbeats received
- [ ] Scale: 50 chargers all connected within X seconds (first measured number, write it down)
- [ ] Same seed → same sequence of events

### Bonus

- [ ] Connect the fleet to a third-party CSMS (SteVe or EVerest/ocpp-csms in Docker) to show interoperability

---

## Week 2: Fault catalog + injection engine

**Done when:** 10–15 faults can be injected by name and seed, each with known ground truth, and each one is reproducible.

- [ ] Fault spec format: id, injected cause, observable symptoms, ground-truth fix, verification check
- [ ] Injection engine (hooks into the charger state machine / config / connection)
- [ ] Ground truth stored separately from what the agent can see
- [ ] Scenario files: seed + list of faults + timing
- [ ] Include "no fault" scenarios (needed to measure false positives)
- [ ] Tests: each fault produces its symptoms, and its fix clears it

Candidate faults (from the OCPP 1.6 spec, to validate):

- Wrong HeartbeatInterval (too long or 0)
- MeterValueSampleInterval = 0 → no meter values
- Charger stuck in Faulted with an error code (ConnectorLockFailure, GroundFailure, HighTemperature, PowerMeterFailure, ReaderFailure, EVCommunicationError)
- Connector stuck in Preparing / Finishing
- Connection drops and reconnect loop
- BootNotification rejected / pending loop
- Clock drift on charger timestamps
- Charger set Unavailable and never set back
- Firmware-hang style fault that only a reboot clears (tests the "no reboot during a session" guardrail)

## Week 3: Agent + MCP tools + guardrails

**Done when:** the agent resolves its first faults end-to-end through MCP tools, and risky actions are blocked or sent for approval.

- [ ] MCP tools: read fleet status, read message log for a charger, get/change config, reset, change availability
- [ ] Guardrails: no Reset with active transaction; dry-run before apply; human approval for risky actions
- [ ] Audit log of every agent action (proposed, approved/blocked, applied, verified)
- [ ] Verify step: the agent must check the fix worked before closing an incident
- [ ] Tests for each guardrail (attempted dangerous action → blocked)

## Week 4: Evaluation campaign (agent vs rules)

**Done when:** one results table with resolution rate, blocked actions, cost and time, for both approaches, on the same scenarios.

- [ ] Rule-based baseline script (same tools, same scenarios)
- [ ] Harness: same scenarios, same seeds, same budget for both
- [ ] Metrics collection + results table
- [ ] Manual review of a sample of agent runs
- [ ] Error analysis: where the agent helps, where it adds nothing

## Week 5: Publish

**Done when:** the repo is public, with the demo video and article linked from the portfolio.

- [ ] README: what it is, quick start, architecture, results, **Limitations**, how AI tools were used
- [ ] Demo video, 60–90 s
- [ ] Results table + blog article
- [ ] Docker setup for one-command demo
- [ ] Portfolio page

---

## Out of scope (v1)

- OCPP 2.0.1
- Web UI or dashboard (terminal + results table only)
- Real database (in-memory / SQLite only)
- TLS, security profiles, authentication
- Smart charging, reservations, firmware updates, OCPI
- Real hardware

## Decisions log

| Date | Decision | Why |
|------|----------|-----|
| 2026-11-24 | OCPP 1.6J only for v1 | Most deployed version; 2.0.1 would double the scope |
| 2026-11-24 | Own Python simulator built on the `ocpp` library, not the SAP Node simulator | Faults must be injected inside the chargers with known ground truth; one language and one process; 100% my code |
| 2026-11-24 | All 50 chargers in one asyncio process | Enough at this scale; simpler to debug and seed |
| 2026-11-24 | JSONL log of every OCPP message | Serves as agent context and audit trail |
| 2026-11-24 | Seeded RNG passed explicitly | Week 4 evaluation must replay identical scenarios |
| 2026-11-24 | Dependencies capped at the current major version; ruff pinned exactly | Avoid unannounced breakage (already hit a master-vs-PyPI mismatch with the `ocpp` examples); new ruff releases change rules/formatting and would break CI on untouched code |
| 2026-11-24 | Plain `websockets` for the CSMS, not FastAPI | OCPP-J is WebSocket-only and the `ocpp` library works directly with `websockets` connections. Revisit only if we need an HTTP API later |
| 2026-11-24 | `hatchling` as build backend; CI matrix Python 3.11 / 3.12 / 3.13 | Minimal config for a src/ layout; 3.13 is the local dev version |

## Open questions

- Simulated clock vs real time for heartbeats and timeouts? (Real time is simpler; a simulated clock makes the evaluation much faster.)
- Which LLM(s) to evaluate in week 4, and with what budget per incident?

## References (inspiration only)

- Mobility House `ocpp` (MIT): core library
- SAP e-mobility-charging-stations-simulator (Apache-2.0): fleet templates design
- docile-charge-point: scriptable charger behavior
- EVerest/ocpp-csms: minimal CSMS on the same library
- SteVe (GPL, read only), CitrineOS, MaEVe: full CSMS references
- `ocpp.md` knowledge base: possible agent context in week 3
