# Account and instrument capability evidence

This milestone resumes the phased project roadmap after the YTD research campaign. It distinguishes four questions: which instruments are listed, which data sources are observed, what a particular account permits, and which strategies/lifecycles this program has implemented and approved.

The audit reads saved artifacts only. It does not authenticate, retrieve credentials, query a broker, enable a service or place orders. Its JSON/Markdown output always has execution authorization and live approval disabled. The dashboard renders the same audit from local evidence.

## Evidence boundaries

- Equities and ETFs share Alpaca's equity asset class. Do not infer ETF classification, exchange membership or index membership from that class or a symbol name. The current directory includes OTC instruments.
- A multi-provider discovery catalog is not the connected broker's execution universe. Provider and observation time matter.
- SIP/OPRA authentication and subscription show connection evidence, not fresh prices or complete market coverage. Idle overnight streams are expected. OPRA contract metadata and a sampled subscription do not establish permission to trade options.
- Account options approval and effective trading levels are separate account fields. [Alpaca options documentation](https://docs.alpaca.markets/us/docs/options-trading) describes these fields. Saved paper-account permissions do not establish live-account permissions.
- Overnight trading requires asset/session eligibility; it cannot be inferred from SIP access. The current saved directory omits some eligibility flags, so those capabilities remain unverified. [Alpaca 24/5 documentation](https://docs.alpaca.markets/us/docs/245-trading-for-trading-api) and [asset attributes](https://docs.alpaca.markets/us/reference/get-v2-assets-1).
- Futures prices currently come from indicative public proxies. There is no configured futures or forex execution adapter in this program. This is a statement about this deployment, not a promise about every broker product.

## Account snapshot

An optional saved account snapshot must be timestamped and identify its paper scope and verified binding. The September 24 snapshot was acquired by the coordinator using existing read-only account/configuration tools and matched to the running paper worker's account. Only allowlisted permission flags were saved; no account ID, account number, balances or credentials are needed for the audit. Stale, malformed, missing or unbound evidence must not become affirmative permission.

The snapshot reports paper options approval and effective level 3. This is evidence of account configuration only. Equities/ETFs and options still require their own frozen strategy, paper execution qualification and reviewed integration with the single account writer. The existing BTC/ETH paper experiment retains its policy. Broader options permission never overrides the proposed future pilot's long-premium-only scope.

## Workflow

Run `scripts/audit_market_capabilities.py --help` for the offline CLI. Supply the artifact root and, optionally, a sanitized timestamped account snapshot. The output directory must be new. Existing execution state and prior reports are never overwritten. The dashboard discovers local `capability_evidence/paper_account_*.json` snapshots and displays the newest saved file; the audit applies its timestamp/binding checks independently.

This is an evidence audit, not a new trading service or an activation checklist that grants authority. Follow-up work remains: retain richer instrument eligibility attributes, complete broker cash/quantity accounting, build options lifecycle tests, then evaluate strategy-specific paper readiness. Windows/WSL startup, private access and single-writer handoff still require tests on the actual PC.
