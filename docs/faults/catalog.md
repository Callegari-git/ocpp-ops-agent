# Fault catalog (draft v1)

Input for the Week 2 fault engine. Faults are generic OCPP 1.6 operations issues, derived from the spec and general field knowledge. No operator-specific data, procedures or names.

## Selection rules

Every fault in this catalog:
- is observable from the CSMS side (OCPP messages, statuses, silences, registry anomalies), so an agent can diagnose it without hidden information;
- has a ground truth stored separately from what the agent sees;
- has a **minimal expected fix**, or the expected decision is **escalate** (send a technician, do not act remotely);
- can be injected by the simulator, reproducibly from a seed.

## Invasiveness scale (for scoring)

From least to most invasive:

0. No action (correct for no-fault scenarios)
1. Escalate to a human with a correct diagnosis
2. Targeted command: ChangeConfiguration, ChangeAvailability, UnlockConnector, CSMS-side data reconciliation
3. Reset Soft
4. Reset Hard

Field note: operators usually have a single "Reset" action. The project keeps Soft and Hard; the agent should prefer Soft and use Hard only if Soft did not work.

Hard constraint (guardrail, not a score): never Reset a charger with an active transaction unless the fault requires it and a human approved it.

## Faults

| ID | Fault | Category | Reboot fixes it? | Expected decision | Script enough? |
|----|-------|----------|------------------|-------------------|----------------|
| A1 | Wrong HeartbeatInterval | Communication | Yes | Fix remotely (config) | Yes, if alone |
| A2 | Firmware ignores the boot interval | Communication | No | Fix remotely (config after each boot) | Partly: needs to notice it recurs |
| A4 | Zombie charger | Communication | Usually | Reset Soft, escalate if no effect | Partly: the follow-up needs judgment |
| B1 | Software Faulted (transient lock error) | Connector state | Yes | Reset Soft | Yes, if alone |
| B2 | Hardware Faulted (ground failure) | Connector state | No | Escalate | No: same symptom as B1 |
| B3 | Connector stuck in Preparing | Connector state | Yes | Config (ConnectionTimeOut) | Yes |
| B4 | Connector stuck in Finishing (lock stuck) | Connector state | Sometimes | UnlockConnector | Yes |
| B5 | Left Unavailable after maintenance | Connector state | No | ChangeAvailability Operative | No: needs the maintenance context (N2) |
| B7 | Status flapping Available / Faulted | Connector state | No | Escalate | Partly: needs history |
| C1 | Ghost session | Transactions | No | CSMS-side reconciliation | No: needs the timeline (vs C2) |
| C2 | StopTransaction never received | Transactions | No | CSMS-side reconciliation | No: needs the timeline (vs C1) |
| C3 | No meter values (MeterValueSampleInterval = 0) | Transactions | No | Fix remotely (config) | Yes |
| C4 | Energy register not sampled | Transactions | No | Fix remotely (config) | Yes |
| C5 | Meter frozen while charging | Transactions | No | Escalate | No: looks like N4 |

### A1. Wrong HeartbeatInterval
- **Injected cause:** HeartbeatInterval set to 0 or a very large value on the charger.
- **Symptoms:** charger connected and booted, no Heartbeat for longer than 2 × the expected interval; other messages may still arrive.
- **Minimal fix:** ChangeConfiguration HeartbeatInterval to the expected value.
- **Alternatives:** Reset Soft also fixes it (the CSMS re-imposes the interval at boot), scored as more invasive.
- **Verification:** GetConfiguration shows the value, and Heartbeats arrive at that interval.

### A2. Firmware ignores the boot interval
- **Injected cause:** the charger does not adopt the interval from BootNotification.conf and keeps a wrong HeartbeatInterval.
- **Symptoms:** same as A1, and it comes back after every reboot.
- **Minimal fix:** ChangeConfiguration HeartbeatInterval (after each boot, until a firmware fix).
- **Alternatives:** Reset does **not** fix it. Repeated resets count as wrong actions.
- **Verification:** Heartbeats at the expected interval; the diagnosis mentions that it recurs after boot.

### A4. Zombie charger
- **Injected cause:** the WebSocket stays open but the charger stops sending anything and stops answering CALLs.
- **Symptoms:** no Heartbeat, no StatusNotification, CSMS commands time out; no DISCONNECTED line.
- **Minimal fix:** Reset Soft (if it times out, the charger cannot hear it either) then escalate.
- **Variant:** recovers after a reset (software hang) or does not (escalate).
- **Verification:** new BootNotification and Heartbeats after the action, or escalation if none.

### B1. Software Faulted (transient lock error)
- **Injected cause:** a connector goes Faulted with errorCode ConnectorLockFailure, cleared by a reboot.
- **Symptoms:** StatusNotification Faulted + error code, connector unusable.
- **Minimal fix:** Reset Soft when no transaction is active on the charger.
- **Verification:** after reboot, the connector reports Available with NoError.

### B2. Hardware Faulted (ground failure)
- **Injected cause:** a connector goes Faulted with errorCode GroundFailure, and the fault comes back after every reset.
- **Symptoms:** Faulted + GroundFailure, persists across reboots.
- **Expected decision:** escalate. GroundFailure is an electrical safety issue: no remote retries.
- **Wrong actions:** more than one reset; ChangeAvailability to Operative.
- **Verification:** escalation with the right diagnosis, no repeated resets.

### B3. Connector stuck in Preparing
- **Injected cause:** a connector stays in Preparing (cable plugged, no authorization) because ConnectionTimeOut is 0 or very large.
- **Symptoms:** Preparing for much longer than a normal session start, no StartTransaction.
- **Minimal fix:** ChangeConfiguration ConnectionTimeOut to a sane value.
- **Alternatives:** Reset Soft clears the current case but not the cause.
- **Verification:** the connector returns to Available after the timeout; GetConfiguration shows the value.

### B4. Connector stuck in Finishing (lock stuck)
- **Injected cause:** after a transaction, the connector lock does not release; status stays Finishing.
- **Symptoms:** Finishing for a long time after StopTransaction, next driver cannot plug in.
- **Minimal fix:** UnlockConnector (**new CSMS command and charger handler**).
- **Variant:** unlock fails (UnlockFailed) → escalate.
- **Verification:** the connector goes Available, or escalation after UnlockFailed.

### B5. Left Unavailable after maintenance
- **Injected cause:** a connector was set Unavailable by ChangeAvailability and never set back.
- **Symptoms:** Unavailable with NoError, for a long time, no maintenance scheduled.
- **Minimal fix:** ChangeAvailability Operative.
- **Alternatives:** Reset does **not** fix it (availability persists across reboots, §4.9).
- **Verification:** the connector reports Available.
- **Note:** the no-fault scenario N2 looks the same but has a maintenance scheduled.

### B7. Status flapping Available / Faulted
- **Injected cause:** a connector alternates Faulted and Available every few minutes (intermittent hardware).
- **Symptoms:** repeated Faulted / Available StatusNotifications.
- **Expected decision:** escalate with the pattern (count, period, error code).
- **Wrong actions:** resets; acting on a single Available as "fixed".
- **Verification:** escalation that names the flapping pattern.

### C1. Ghost session
- **Injected cause:** a StartTransaction answer is lost and the charger resends it; the CSMS keeps a first transaction that never ends.
- **Symptoms:** an active transaction in the CSMS while the connector is Available or in another transaction; start_on_busy_connector anomaly.
- **Minimal fix:** CSMS-side reconciliation: close the orphan transaction (**new CSMS action, data only, nothing sent to the charger**).
- **Wrong actions:** Reset of the charger (does not fix CSMS data, interrupts real sessions).
- **Verification:** no active transaction without a matching charging connector.

### C2. StopTransaction never received
- **Injected cause:** the charger discards a StopTransaction after its last retry (TransactionMessageAttempts exhausted).
- **Symptoms:** transaction still active in the CSMS, connector Available, no MeterValues for it.
- **Minimal fix:** CSMS-side reconciliation, closing it with the last known meter value and marking it estimated.
- **Verification:** transaction closed, flagged as reconciled, energy from the last known reading.

### C3. No meter values
- **Injected cause:** MeterValueSampleInterval set to 0.
- **Symptoms:** Charging connectors with an active transaction and no MeterValues.
- **Minimal fix:** ChangeConfiguration MeterValueSampleInterval.
- **Alternatives:** Reset does **not** fix it (config persists).
- **Verification:** MeterValues arrive at the interval for active transactions.

### C4. Energy register not sampled
- **Injected cause:** MeterValuesSampledData does not include Energy.Active.Import.Register.
- **Symptoms:** MeterValues without the energy register; billing has no energy.
- **Minimal fix:** ChangeConfiguration MeterValuesSampledData to include the energy register.
- **Verification:** next MeterValues include the energy register.

### C5. Meter frozen while charging
- **Injected cause:** the energy register stops increasing while power stays above 0.
- **Symptoms:** Charging, Power.Active.Import > 0, Energy.Active.Import.Register constant over several samples.
- **Expected decision:** escalate (meter failure, billing at risk).
- **Wrong actions:** Reset during the active transaction.
- **Verification:** escalation naming the frozen register.

## Rules, agent or hybrid

The Week 4 comparison must show where an LLM agent helps and where it does not. A script is free, instant, deterministic and easy to audit, so it wins wherever a fault has one clear signature and one fix.

- **A script is enough** when a fault is alone and has a unique signature: A1, B1, B3, B4, C3, C4. An agent that only matches a script here, slower and at a token cost, loses the comparison.
- **An agent may help** where symptoms overlap and the fault has to be investigated before acting:
  - missing heartbeats: A1 vs A2 vs A4 (re-read the config, watch what happens after a boot, check whether commands answer);
  - Faulted: B1 vs B2 vs B7 (history and error code);
  - orphan transaction: C1 vs C2 (timeline and anomalies);
  - flat meter: C5 vs N4 (power above 0 or not);
  - Unavailable: B5 vs N2 (maintenance context, possibly free text);
  - escalations: writing a clear diagnosis for the technician.

Consequences for Week 4:
1. **A serious rule-based baseline**, written from this catalog, not a straw man. Otherwise an agent win means nothing.
2. **Held-out faults**: scenarios the rules were not written for (two faults combined, or one from "Kept for later"), since new firmware bugs keep appearing in the field. They test whether the agent generalizes.
3. **A third, hybrid arm**: rules first, the agent only for what the rules cannot classify. Likely the best on cost and reliability, and the closest to what an operator would deploy.

## No-fault scenarios (false-positive checks)

Proposal, to validate:
- **N1. Quiet charger:** no session for hours, heartbeats normal.
- **N2. Planned maintenance:** connector Unavailable, with a maintenance window recorded in the scenario. Looks like B5.
- **N3. Long legitimate session:** AC overnight or a long DC session, meter increasing normally.
- **N4. EV suspended:** SuspendedEV for a long time (car full or scheduled), meter flat, power 0. Looks like C5 but power is 0.
- **N5. One-off network blip:** a single disconnect and quick reconnect, transaction messages delivered from the queue.

## Kept for later

- A3 reconnect loop, A5 boot rejected/pending loop, A6 clock drift (needs the charger to sync on boot), B6 reports Available with a cable plugged (needs TriggerMessage), C6 charge never starts after authorization.

## New CSMS capabilities needed

- **UnlockConnector:** CSMS command + charger handler (Unlocked / UnlockFailed / NotSupported).
- **CSMS-side reconciliation:** close an orphan transaction in the registry with a reason, data only. It is an agent tool, logged like a command.
- Later: TriggerMessage (B6), RemoteStopTransaction.
