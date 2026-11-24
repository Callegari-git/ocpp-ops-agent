# OCPP Operations Agent

An open-source simulator of an OCPP 1.6J charging-station fleet that injects realistic faults, plus an AI agent that diagnoses, verifies and fixes them under guardrails. The agent is benchmarked against a rule-based script on the same reproducible scenarios, to measure where it helps and where it doesn't.

**Status:** work in progress. See [ROADMAP.md](ROADMAP.md) for the plan and current progress.

## Quick start

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
make test
make lint
```

## Data and IP

This project uses no TotalEnergies data, configuration or code. Simulated faults are derived from the OCPP 1.6 specification and general knowledge of charging-network operations.

## License

[MIT](LICENSE)
