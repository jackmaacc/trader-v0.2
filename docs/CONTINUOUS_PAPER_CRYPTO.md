# Continuous paper crypto management

This service manages the explicitly authorized Alpaca **paper** experiment. It is not proof of a profitable edge, a live-money deployment, a high-frequency strategy, or all-market coverage.

## Current scope and exit

The worker checks account/order health every 60 seconds, including weekends. It evaluates **BTC/USD and ETH/USD** against their latest 200 consecutive completed UTC daily closes from Alpaca. It holds or considers entering when the last close is strictly above the arithmetic average; it sells its owned available quantity when that close is at or below the average. Exits take priority over entries. A change during an unfinished daily bar does not trigger this rule. There is no intraday protective stop, so a position can remain open for days or weeks and can incur substantial losses before the daily signal changes.

This is a scheduled daily-signal paper experiment. Polling every minute does not create a minute-by-minute predictive edge. The original BTC entry was later than the backtest's next-open convention, and future executions occur on the first successful check after completed daily data become available.

New entries target 12.5% of current equity, capped at $12,400 each, with a 25% aggregate entry exposure cap and a crypto fee cash reserve. Existing positions can appreciate above that entry cap. The inherited 3% daily-loss rule uses equity at the first successful check of each UTC day (or initial service start), rather than reconstructing midnight equity, and blocks new entries; it is not a guaranteed loss cap and does not liquidate holdings. Entry quotes must be no more than 60 seconds old, uncrossed, and have a spread no wider than 25 basis points. Entries use immediate-or-cancel price limits; an unfilled entry is not chased repeatedly on the same signal day. Signal exits use market orders and can slip.

The worker must verify the bound paper account and reconcile durable order intentions before trading. The seed adopts only the known BTC paper entry from September 24 UTC, using its actual net received quantity. It does not claim ownership of unrelated positions. Unknown holdings, orders or ambiguous responses block new trading instead of being silently adopted. Credentials are read into memory from the existing macOS Keychain entry; none are embedded in source, launch configuration, logs or reports.

## Supervision and visibility

The macOS LaunchAgent label is `com.trader-v02.paper-crypto`. Its deployment is local to this Mac and user session. `launchd` supervises crashes; a clean intentional shutdown is not automatically restarted. The launcher uses `caffeinate -s` to prevent idle system sleep while on AC power. Closing the laptop lid, logging out, shutdown, loss of power/network, unavailable Keychain access, broker failures or missing market data can still interrupt management. This is not a cloud uptime guarantee.

The worker's durable state and `status.json` are under `artifacts/paper_crypto_service/`. The dashboard reads that status every 30 seconds and flags a heartbeat older than three minutes. A separate read-only Codex watchdog checks for actionable failures; it never becomes a competing order writer. Watchdog delivery itself depends on Codex scheduling and availability.

To prevent further entries while keeping daily exits enabled, create `artifacts/paper_crypto_service/STOP`. To stop the worker entirely, create `artifacts/paper_crypto_service/SHUTDOWN`; this leaves any positions open and disables their automatic management. Stopping the service is not the same as closing the portfolio. Do not launch another order writer against this account while this worker owns its account lock.

## Expansion

Other crypto assets, equities, options, forex and futures are not automatically enabled. Supporting an instrument catalog is different from having valid signals and correct execution/accounting for it. Further markets need suitable historical and current data, instrument-specific order/settlement handling, cost-aware validation, and explicit managed exits before joining this worker. No self-tuning or promotion of rejected strategies occurs in the running service.

## Deployment verification — September 24, 2026 UTC

The supervisor was installed in the user's `~/Library/LaunchAgents/com.trader-v02.paper-crypto.plist` and bootstrapped successfully. Broker-connected observation completed before enabling orders. The first two execution-enabled management cycles at approximately 03:02:59 and 03:04:03 UTC verified the existing 0.146629544 BTC holding, returned hold under the positive daily signal, retained the prior ETH entry attempt, and reported no pending orders or errors. These cycles did not submit additional orders. The actual saved account state and quantities can change after this verification.

The read-only watchdog `watch-paper-crypto-manager` is active at 15-minute intervals. The dashboard status panel rendered successfully against the worker's real status. Independent execution review passed; the coordinator's combined focused validation passed 139 tests covering the planner, service, health UI, existing broker/lifecycle/recovery components, and dashboard integration. Simulated fault tests cover exits, partial fills, restart reconciliation, ambiguous submission, and durable-write failure; no real sell was forced solely to test the exit path.

Reproduction and observation:

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_crypto_manager.py tests/test_paper_crypto_service.py tests/test_crypto_service_ui.py tests/test_paper_support.py tests/test_paper_lifecycle.py tests/test_paper_resume.py tests/test_market_catalog.py tests/test_specialist_dashboard.py -q
launchctl print gui/$(id -u)/com.trader-v02.paper-crypto
```

Do not run a second service while the supervisor is active. The command-line worker defaults to observation and requires `--execute-paper` for broker mutation. Exact local deployment arguments are retained in `artifacts/paper_crypto_service/supervisor.plist`; the seed, account-bound state, logs and deployment artifacts remain excluded from Git.
