# Phase 0 acceptance — September 24, 2026

Scope: complete **Mandate and baseline**, then stop. The roadmap defines its deliverables as the mandate/roadmap, service inventory, strategy authority register and milestone tracking; its exit gate is that every component has an owner, purpose and activation boundary. The pending account/instrument capability baseline has also been delivered. This acceptance does not substitute later-phase investment, execution, accounting or host-deployment requirements with a checklist.

## Requirement-to-evidence record

| Phase 0 requirement | Authoritative evidence | Verification |
|---|---|---|
| Accepted investment and deployment mandate | `PROJECT_ROADMAP.md`; `configs/project_mandate.json` | Absolute growth objective; Mac development; at least 30 paper days; future WSL2 host; proposed pilot capital/risk and parallel asset research are explicit. JSON purpose is planning-only; live activation, investment approval and automatic promotion are false. |
| Service inventory and purpose | `COMPONENT_INVENTORY.md` | Six supervised workers, dashboard and watchdog have named purposes, outputs and control boundaries. Read-only launchctl inspection confirmed the six known workers running; it made no service changes. |
| Every component has an engineering owner | Inventory's accountable-owner table; `.codex/agents/`; `AGENT_TEAM.md` | Covers all top-level `src/trader_engine` module families, services, CLI/research tools, compiled backtest core, legacy order-capable scripts, tests, deployment/configuration and evidence. Jack retains operator/activation decisions; specialist roles are engineering assignments, not extra executors. |
| Strategy authority register | Inventory's Strategy authority section | BTC/ETH is the existing limited paper experiment; breakout diagnostic only; MR/MOM failures retained; H1–H4 unqualified; options research-only; futures indicative; live not activated. |
| Account/instrument capability baseline | `MARKET_CAPABILITY_AUDIT.md`; `operations/capability_audit.py`; `scripts/audit_market_capabilities.py`; saved report | Separates discovery, observed subscription/data freshness, paper account flags, existing adapters and strategy approval. Saved evidence lists 13,495 equity-class instruments, 12,270 sampled option contracts, 36 crypto instruments and eight indicative futures. Missing flags and unsupported integrations are explicit. |
| Permission provenance and authority boundary | Sanitized bound paper account snapshot; saved report; negative tests | Paper account level 3 is observed configuration, not live permission or strategy approval. Missing, stale, unbound, inactive, malformed and blocked evidence cannot grant readiness. All report execution/live flags remain false. |
| Usable dashboard baseline | `ui/capability_audit.py`; `ui/project_operations.py` | Local-only capability panel integrated; real-artifact Streamlit AppTest rendered with zero exceptions. Data authentication remains separate from fresh market prices. |
| Milestone tracking and remaining work | Roadmap phases/exit gates; inventory's foundation gaps | Phase 0 complete; later phases retain their existing status and acceptance requirements. Pre-existing Phase 1 groundwork and research artifacts do not imply completion of those phases. |
| Independent review and regression evidence | 24 focused tests; specialist review; local verification record | Audit, capability UI and project-operations tests pass. Independent review corrected malformed/inactive-account handling and approved the read-only implementation. No order policy, service or broker setting changed. |

## Reproduction and local evidence

```sh
.venv/bin/python -m pytest tests/test_capability_audit.py tests/test_capability_audit_ui.py tests/test_project_operations_ui.py -q
.venv/bin/python scripts/audit_market_capabilities.py --artifacts artifacts --account-snapshot artifacts/capability_evidence/paper_account_20260924.json --output artifacts/NEW_capability_audit
```

The report destination must be new. The saved account snapshot is time-bounded; rerunning much later can correctly report it stale. The original audit at 05:18:55 UTC remains a historical baseline, not a perpetual permission assertion. Outputs stay outside Git:

- `artifacts/capability_audit_20260924/report.json` and `REPORT.md`.
- `artifacts/phase0_acceptance_20260924/verification.json`: dashboard and read-only supervisor verification.
- `artifacts/capability_evidence/paper_account_20260924.json`: sanitized permission evidence, no credentials/account identifiers/balances.

## Stop boundary

No further phase is started by this acceptance. Existing authorized paper/data/observer services continue under their prior mandates; “stop after Phase 0” stops project expansion, not the management of existing paper holdings. No new strategy, order writer, options execution, risk policy, live activation, PC installation or service restart is included.

Richer instrument/session metadata, recurring permission capture, complete broker cash/quantity accounting, off-machine recovery, actual PC boot/private-access tests, unified execution/options lifecycle and prospective strategy qualification remain later-phase work. Identifying these gaps is part of a truthful baseline; closing them is not claimed here.
