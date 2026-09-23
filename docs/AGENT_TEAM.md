# Project agent team

Eleven reusable Codex specialist definitions complement the five existing application advisory specialists (technical, fundamental, news, macro and bear). The parent agent is the coordinator. These files are development roles, not additional automated trading signal votes or an always-running trading system.

## Roster

| Agent | Responsibility |
|---|---|
| `data_quality` | Validate market data and diagnose quote rejection. |
| `execution_engineer` | Repair broker order lifecycle and recovery. |
| `portfolio_risk` | Review account-wide sizing and concurrent exposure. |
| `research_designer` | Design falsifiable trading experiments and evaluate evidence. |
| `simulation_runner` | Run reproducible simulation campaigns and package complete evidence. |
| `backtest_auditor` | Audit causality and simulation realism. |
| `trade_reconciler` | Reconcile orders, fills, positions and reported performance. |
| `reliability_engineer` | Diagnose concurrency, latency and operational failures. |
| `test_engineer` | Build meaningful offline regression and fault-injection tests. |
| `dashboard_engineer` | Improve research dashboard clarity and artifact presentation. |
| `independent_reviewer` | Challenge completed changes and release evidence. |

## Routing and coordination

- Quote or feed incident: data_quality + reliability_engineer, then execution_engineer and test_engineer for an agreed fix.
- Broker lifecycle change: execution_engineer + portfolio_risk + test_engineer; independent_reviewer checks the stable result.
- Strategy experiment: research_designer freezes the design; simulation_runner executes it with data_quality support; backtest_auditor and trade_reconciler independently review the evidence.
- Repeat simulation request: simulation_runner owns the campaign; see docs/SIMULATION_AGENT.md.
- Results/dashboard change: trade_reconciler + dashboard_engineer; test_engineer checks affected behavior.

Use only roles needed by the task. The setup session exposes four total slots: coordinator plus three workers. Run larger teams in waves. This is a session limit, not a promise that all eleven run concurrently. No global concurrency/model settings were changed. Roles inherit model settings unless the host applies another configuration.

Every assignment states objective, input evidence, owned files or read-only scope, forbidden side effects, deliverable and completion checks. Every handoff returns findings with file/line evidence, changed files, commands/results, unresolved assumptions and dependencies. Assign one writer per file. Wait for dependent writers before final review. Separate tasks sharing the checkout must coordinate ownership too.

## Persistence and use

Standalone TOML definitions live in `.codex/agents/`, following the official [OpenAI subagent configuration](https://learn.chatgpt.com/docs/agent-configuration/subagents). `AGENTS.md` tells future project tasks to use the team for substantial independent work. Host discovery of the new named roles is not verified by TOML parsing; an already-running task may need a fresh session to load configuration. When the runtime exposes only generic spawn tools, the coordinator can read the role file and supply its instructions in the assignment.

Example request: “Use the project agent team to diagnose quote failures, implement the verified fix, and have an independent reviewer check it.”

The initial setup actually dispatched three bounded read-only subagents for data quality, execution/recovery and research/test architecture. Their findings are recorded in `docs/AGENT_TEAM_INITIAL_REVIEW.md`. This dispatch used explicit assignments while the reusable definitions were being created.
