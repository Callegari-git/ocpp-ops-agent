# Roadmap: OCPP Operations Agent

## Goal

Show, with public code and measured numbers, that an AI agent can run real charging-network operations (diagnose, verify, fix) safely, and show where it beats a rule-based script and where it doesn't.

- **Budget:** 5 weeks, ~15–20 h/week (80–100 h total)
- **Target:** public v1 (repo + demo video + results + article) before end of January 2027
- **Started:** 2026-11-24
- **Last updated:** 2026-11-24

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
- [x] GitHub Actions CI (lint + tests) running on every push (workflow written; tick after the first green run)
- [x] Minimal README, including the "no TotalEnergies data, config or code" statement
- [x] `.gitignore` (incl. `logs/`), MIT license

### CSMS

- [x] Handlers: BootNotification, Heartbeat, StatusNotification, MeterValues
- [x] In-memory charger registry: status per connector, last heartbeat, known config
- [x] Outbound commands: GetConfiguration, ChangeConfiguration, Reset (future agent tools)
- [x] JSONL logging of every message in and out

### Virtual charger

- [x] Explicit state machine (Available, Preparing, Charging, SuspendedEV, SuspendedEVSE, Finishing, Unavailable, Faulted), single transition point
- [x] Config store with standard 1.6 keys (HeartbeatInterval, MeterValueSampleInterval, ConnectionTimeOut, ...)
- [x] Boot → heartbeat loop → status notifications on transitions
- [x] Respond correctly to GetConfiguration, ChangeConfiguration, Reset

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

- [ ] Charging sessions: CSMS handlers (Authorize, StartTransaction, StopTransaction, MeterValues during a transaction) + virtual charger sessions (Preparing → Charging → Finishing through `set_status`). Needed first: the "no reboot during a session" guardrail and several faults depend on it
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
| 2026-11-24 | JSONL logging via a `LoggingConnection` wrapper around the websocket (logs in `recv()`/`send()`), not by overriding `ChargePoint` internals | `ChargePoint` only calls `recv()`/`send()` on its connection, so the wrapper sees every frame and survives library upgrades. Outgoing frames are logged *before* sending, so a fast reply can always be matched to its CALL |
| 2026-11-24 | Injected clock (`now` function) in the registry, handlers and log | Tests control time; a simulated clock can replace real time later without touching callers |
| 2026-11-24 | Each connection gets a registry token; a close only marks the charger disconnected if its token is still the current one | A charger can reconnect before its old connection is detected as dead; the old one closing must not mark the live one offline |
| 2026-11-24 | Connector state keeps both the charger's `timestamp` and our `received_at` | Makes the week-2 clock-drift fault observable |
| 2026-11-24 | Unparseable frames are logged as `INVALID` with the raw text, not dropped | The audit trail must contain everything that crossed the wire |
| 2026-11-24 | `CentralSystem` class owns live sessions and outbound commands (`get_configuration`, `change_configuration`, `reset`); typed results, `CommandError` subclasses for not connected / timeout / CALLERROR | One object for the future agent tools to call; `call(suppress=False)` because ocpp otherwise returns `None` on a CALLERROR |
| 2026-11-24 | CSMS commands are mechanism only, no safety logic | Guardrails live in the agent layer (week 3), so the rule-based baseline and the agent use the same raw tools |
| 2026-11-24 | ChangeConfiguration records the charger's answer in `config_changes` but never updates `known_config`, even when Accepted | `known_config` is only what the charger reported via GetConfiguration; the week-3 verify step must re-read, not trust our assumption |
| 2026-11-24 | `known_config` stores value **and** readonly per key | The agent must know a key is read-only before trying to change it |
| 2026-11-24 | CONNECTED / DISCONNECTED events in the same JSONL file, same line shape (`direction`/`message_id`/`action` null); disconnect reason `clean` / `network_drop` (code 1006) / `error` | One format for readers; the agent needs connection history for the reconnect-loop fault |
| 2026-11-24 | Pending CALL ids in the log wrapper expire after 2 × call timeout | Unanswered CALLs would otherwise accumulate for the life of the connection; 2× (not 1×) so a late answer is still logged with its action |
| 2026-11-24 | Connector transitions as a table (`ALLOWED_TRANSITIONS`, from OCPP 1.6 §4.9), no Reserved; connector 0 limited to Available/Unavailable/Faulted; same-status re-report allowed; Faulted requires an error code | One readable source of truth, testable on its own; reservations are out of scope. Table to be checked cell by cell against the spec PDF |
| 2026-11-24 | `VirtualCharger.set_status()` is the only path for status changes; connector state is a frozen dataclass, replaced not edited | Week 2's fault engine hooks in at one place; nothing can change a status behind its back |
| 2026-11-24 | Charger takes injected `now`, `sleep` and a seeded `random.Random`; tests use `ManualTime` (virtual time that moves only when the test advances it) | Fast tests with exact timing assertions, and reproducible runs. A first step toward the simulated-clock open question |
| 2026-11-24 | Charger message ids come from the seeded RNG (passed via `call(unique_id=...)`) instead of ocpp's default `uuid4()` | Same seed must give the same run, message ids included |
| 2026-11-24 | Reconnect backoff: `min(60, 2^failures) × uniform(0.5, 1.0)` s from the seeded RNG; not websockets' built-in reconnect loop | Jitter avoids a fleet reconnecting in lockstep; the built-in loop uses real `asyncio.sleep` and unseeded randomness |
| 2026-11-24 | ChangeConfiguration rejects badly formatted values (non-integer / negative ints, non-boolean bools) | A value the charger itself cannot use (e.g. `HeartbeatInterval=abc`) must not crash it |
| 2026-11-24 | Charger config persists across Reset (lives on the charger, not the connection); connector statuses are kept too | Non-volatile on real chargers; otherwise a reset would silently "fix" config faults. What a reboot clears is decided per fault in week 2 |
| 2026-11-24 | On Ctrl+C the charger closes with 1001 (going away) | websockets closes with 1011 (internal error) when an exception leaves the connection block; the CSMS would log a clean stop as an error |

## Open questions

- Simulated clock vs real time for heartbeats and timeouts? (Real time is simpler; a simulated clock makes the evaluation much faster.)
- Which LLM(s) to evaluate in week 4, and with what budget per incident?
- Messages before an accepted BootNotification: the CSMS currently accepts them. Should it reject them (OCPP 1.6 §4.2)? Decide in week 2 together with the Pending/Rejected boot faults.
- SIGTERM (Docker stop, week 5) is not handled yet; only Ctrl+C/SIGINT shuts down cleanly.
- Dead-connection detection time depends on websockets keepalive (`ping_interval` / `ping_timeout`, both 20 s by default, so up to ~40 s). Week 2: make them configurable and measure "time to detect a dead charger".
- One command at a time per charger (ocpp's `call()` lock): a silent charger blocks all commands to it until the call timeout (default 30 s). Is 30 s right for the agent's time budget (week 3/4)?
- Real firmwares sometimes send out-of-spec transitions. Week 2 may need a "buggy firmware" fault that bypasses the transition table on purpose, and the CSMS must record such transitions without crashing (and maybe flag them). Not built yet.
- HeartbeatInterval is re-imposed by the CSMS at every boot (BootNotification.conf `interval`, OCPP 1.6 §4.2), so any reboot "fixes" a wrong HeartbeatInterval, as on real chargers. For the week-2 HeartbeatInterval fault: inject it so it survives a reboot (e.g. a firmware that ignores the boot interval), or accept that Reset is a legitimate fix for it?
- `ManualTime` lives in `tests/` for now. Promote it to `src/` as the simulated clock for week-4 evaluation runs?

## References (inspiration only)

- Mobility House `ocpp` (MIT): core library
- SAP e-mobility-charging-stations-simulator (Apache-2.0): fleet templates design
- docile-charge-point: scriptable charger behavior
- EVerest/ocpp-csms: minimal CSMS on the same library
- SteVe (GPL, read only), CitrineOS, MaEVe: full CSMS references
- `ocpp.md` knowledge base: possible agent context in week 3
