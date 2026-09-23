# TRADER2.0 project agents

The user requests ongoing use of specialized agents throughout this project. For substantial work with independent subtasks, delegate to the relevant roles in `.codex/agents/` and coordinate their results. See `docs/AGENT_TEAM.md` for role selection and handoffs. Simple tasks can remain local. Respect the available session concurrency limit; rotate specialists in bounded waves rather than spawning redundant work.

Read current code and project documentation before changes. The existing technical, fundamental, news, macro and bear advisory specialists in `src/trader_engine/agents/` remain application components; the new Codex roles develop and assess the project.

The coordinator assigns each writer exclusive files, gathers evidence, integrates changes and obtains independent review for material execution/risk changes. Start independent readers in parallel. Do not edit a file another task owns; agree a handoff first. Preserve user changes and ongoing work in other tasks.

Default engineering validation uses `.venv/bin/python -m pytest` with focused test paths and offline fixtures. Report exact checks and remaining uncertainty. Do not launch trading scripts or network-dependent experiments as tests. Research evidence and test passes do not establish profitability or authorize broker actions.

Generic agent setup, engineering and research requests do not authorize orders, cancellations, trading restarts, credential access or broker-setting changes. Where separately authorized, retain a single execution owner and the existing durable ownership/reconciliation controls. Preserve the user-approved policy for the specific runner; do not silently replace it with limits from another profile. Keep secrets out of source, prompts and reports.

Route requested backtests, execution-sensitivity campaigns and repeated simulation runs to `simulation_runner`; read `docs/SIMULATION_AGENT.md`. Use the existing shared engine, complete the requested run count, and obtain independent evidence review. Role setup alone does not launch or schedule simulations.
