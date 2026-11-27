# Roadmap: OCPP Operations Agent

## Goal

Show, with public code and measured numbers, that an AI agent can run real charging-network operations (diagnose, verify, fix) safely, and show where it beats a rule-based script and where it doesn't.

- **Budget:** 5 weeks, ~15–20 h/week (80–100 h total)
- **Target:** public v1 (repo + demo video + results + article) before end of January 2027
- **Started:** 2026-11-24
- **Last updated:** 2026-11-27

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

- [x] `fleet.yaml` format (charger templates: vendor, model, connectors, count, id prefix; no heartbeat interval, the CSMS sets it at boot)
- [x] Launcher: N chargers in one asyncio process, seeded start-up jitter; embedded CSMS by default, `--csms-url` for an external one
- [x] Live terminal status view (compact grid + Attention list, fits 100×30)

### Tests

- [x] Integration: CSMS + 3 chargers → boot accepted, heartbeats received
- [x] Scale: 50 chargers all booted within 15 s (CI bound). **Measured: 0.060 s** median of 10 runs (0.059–0.065 s) for boot + every StatusNotification of all 50. Context: single process, localhost, embedded CSMS, jitter 0 (all 50 at once), Apple M5 / 24 GB / macOS 26.6, Python 3.13.3, ocpp 2.1.0, websockets 17.4. This is a simulator-throughput number, not a field number: the README **Limitations** section must say so when we publish
- [x] Same seed → same sequence of events (and adding a charger leaves the others' sequences unchanged)

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

- [ ] Rule-based baseline script (same tools, same scenarios). Detection half: reuse `fleet.status_rows.attention()` as is
- [ ] Harness: same scenarios, same seeds, same budget for both
- [ ] Metrics collection + results table
- [ ] Manual review of a sample of agent runs
- [ ] Error analysis: where the agent helps, where it adds nothing

## Week 5: Publish

**Done when:** the repo is public, with the demo video and article linked from the portfolio.

- [ ] README: what it is, quick start, architecture, results, **Limitations** (incl. the context of the week-1 scale number: single process, localhost, jitter 0, dev machine), how AI tools were used
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
| 2026-11-27 | Reset is a legitimate fix for a wrong HeartbeatInterval. Week-2 fault specs define a "minimal expected fix" per fault; the evaluation scores both resolution and invasiveness (targeted ChangeConfiguration < Reset). Week 2 also includes faults a reboot does not fix (e.g. firmware ignoring the boot interval) | The CSMS re-imposes HeartbeatInterval at every boot (BootNotification.conf `interval`, OCPP 1.6 §4.2), as on real chargers, so a reboot really does fix it. Scoring invasiveness keeps "reboot everything" from looking as good as a targeted fix |
| 2026-11-27 | Fleet file: templates (id prefix, vendor, model, connectors, count) + global seed, CSMS heartbeat interval, start-up jitter window; Pydantic with `extra="forbid"`; uniqueness checked on the generated ids | Typos must fail loudly; different prefixes can still produce the same id (`CP-` ×1001 vs `CP-1`). No per-template heartbeat interval: the CSMS sets it at boot |
| 2026-11-27 | One RNG per charger, seeded with `sha256(f"{fleet_seed}/{charger_id}")` (first 8 bytes); its first draw is the start-up delay, `uniform(0, window)` | A shared RNG would make every charger's behavior depend on the others; sha256 is stable across processes and Python versions, unlike `hash()` |
| 2026-11-27 | Embedded mode logs the CSMS side only (`logs/csms-*.jsonl`); `--csms-url` mode logs the charger side (`logs/fleet-*.jsonl`) | Every message is logged once in each mode, without writing every frame twice |
| 2026-11-27 | Live view in plain text + ANSI codes (alternate screen, redraw in place), no library; summary line only when stdout is not a TTY; INFO logging off while it is on | No new dependency; pipes and CI get readable lines; log lines would garble the redraw |
| 2026-11-27 | Fleet tasks stop before the embedded CSMS on Ctrl+C | Each charger closes with 1001 while the server is still up, so the CSMS log shows clean stops |
| 2026-11-27 | Scale test measured with jitter 0 and marked `slow` (skippable locally with `-m "not slow"`, run in CI) | Jitter 0 is the worst case for the CSMS; with the default 10 s window the time would only measure the jitter |
| 2026-11-27 | Compact grid instead of one row per charger: summary + legend, one cell per charger (id + one dot per connector 1..N), Attention list (max 5, then "… N more"). Width read at every refresh | 50 rows don't fit a terminal, and this view is the README hero GIF. At 100 columns the 50-charger fleet takes ~18 lines |
| 2026-11-27 | Dot colors: green Available, blue Preparing…Finishing, yellow Unavailable, red Faulted, gray offline / not booted / never reported. The id is colored by connector 0 (red Faulted, yellow Unavailable, gray offline), default color when Available | A grid of green ids would be unreadable; connector 0 is the charge point as a whole (OCPP 1.6 §4.9), so it belongs on the id |
| 2026-11-27 | Colors off when `NO_COLOR` is set (no-color.org) or stdout is not a TTY; without colors, connectors are letters (A/B/U/F/-) | Identical dots would carry no information without color |
| 2026-11-27 | Attention severity: offline > Faulted > connected-not-booted > late heartbeat (more than 2 × interval since the last heartbeat, or since boot if none since); one line per charger with every reason, sorted by its worst. Offline or not-booted chargers get no other reason, since their last statuses may be stale | Blind (offline) is worse than a known fault; the boot fallback catches a charger that never heartbeats; the "latest of heartbeat and boot" rule avoids flagging a just-rebooted charger |
| 2026-11-27 | `attention()` (in `fleet/status_rows.py`) is pure and free of any display concern; rendering lives in `fleet/status_grid.py` | It is effectively the **detection half of the week-4 rule-based baseline** and must be reusable there as is |
| 2026-11-27 | After every ocpp `call()` (charger `ChargerChargePoint.send()`, CSMS `CentralSystem._call()`), `raise_if_cancelling()` (`ocpp_ops/cancellation.py`) re-raises a cancellation that was swallowed | CPython **gh-86296**: on Python 3.11, `asyncio.wait_for()` returns the result instead of raising `CancelledError` when both arrive in the same loop iteration (fixed in 3.12 by rewriting `wait_for` on `asyncio.timeout()`). ocpp 2.1.0 waits for every answer with `wait_for` (**`ocpp/charge_point.py:455`**, in `_get_specific_response`). On 3.11 CI it made a stopping charger carry on booting and start its heartbeat in a TaskGroup that was shutting down (RuntimeError + "never awaited" warning); it could also hang a stopping session for `call_timeout`. The check is `Task.cancelling() > 0`, which 3.11's `wait_for` never resets. **Remove when 3.11 is dropped or ocpp stops using `wait_for`**: the tests below will still pass, since they recreate the behavior themselves |
| 2026-11-27 | `VirtualCharger._restart_heartbeat()`, the only place that adds a task to a running session, starts nothing when the calling task is being cancelled, and creates the coroutine only after that check | A TaskGroup that is shutting down cancels all its tasks and refuses new ones. Checking our own task's cancellation avoids reading TaskGroup's private `_aborting`. Also covers the ChangeConfiguration path, which is safe today only because its handler is synchronous |
| 2026-11-27 | The 3.11 behavior is recreated in tests by patching ocpp's `call()` (real answer, then cancel + swallow), for the boot, ChangeConfiguration and CSMS command paths | Deterministic on every Python version. The real race needs a slow machine: the scale test passed 20/20 on local 3.11 before the fix. 3.11 stays in the CI matrix |

## Open questions

- Simulated clock vs real time for heartbeats and timeouts? (Real time is simpler; a simulated clock makes the evaluation much faster.)
- Which LLM(s) to evaluate in week 4, and with what budget per incident?
- Messages before an accepted BootNotification: the CSMS currently accepts them. Should it reject them (OCPP 1.6 §4.2)? Decide in week 2 together with the Pending/Rejected boot faults.
- SIGTERM (Docker stop, week 5) is not handled yet; only Ctrl+C/SIGINT shuts down cleanly.
- Dead-connection detection time depends on websockets keepalive (`ping_interval` / `ping_timeout`, both 20 s by default, so up to ~40 s). Week 2: make them configurable and measure "time to detect a dead charger".
- One command at a time per charger (ocpp's `call()` lock): a silent charger blocks all commands to it until the call timeout (default 30 s). Is 30 s right for the agent's time budget (week 3/4)?
- Real firmwares sometimes send out-of-spec transitions. Week 2 may need a "buggy firmware" fault that bypasses the transition table on purpose, and the CSMS must record such transitions without crashing (and maybe flag them). Not built yet.
- `ManualTime` lives in `tests/` for now. Promote it to `src/` as the simulated clock for week-4 evaluation runs?
- The registry stores `boot_time` but not the boot status: the status view infers "booted" from `boot_time` because our CSMS accepts every boot. Week 2 (Pending/Rejected faults) needs a real `boot_status` field.
- On Python 3.12+, an exception raised by a session task while the session is being stopped is replaced by `CancelledError`: the 3.11 bug above was invisible there except as close code 1011 on the CSMS side. Log session-task errors explicitly (week 2, with the fault engine)?
- ocpp runs `@after` handlers (our `after_reset` → `reboot()`) with `asyncio.ensure_future`, outside the session TaskGroup. Harmless today (`reboot()` only closes the socket), but untracked: keep `@after` handlers trivial.
- `attention()` does not flag a connector (or connector 0) left Unavailable: it only shows as a yellow dot/id. The week-2 "set Unavailable and never set back" fault needs a rule for it (Unavailable for longer than X?) in the week-4 baseline.

## References (inspiration only)

- Mobility House `ocpp` (MIT): core library
- SAP e-mobility-charging-stations-simulator (Apache-2.0): fleet templates design
- docile-charge-point: scriptable charger behavior
- EVerest/ocpp-csms: minimal CSMS on the same library
- SteVe (GPL, read only), CitrineOS, MaEVe: full CSMS references
- `ocpp.md` knowledge base: possible agent context in week 3
