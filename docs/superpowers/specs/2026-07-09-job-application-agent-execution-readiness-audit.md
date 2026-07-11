# Job Application Agent Execution Readiness Audit

Date: 2026-07-09
Audit target:
- `docs/superpowers/specs/2026-07-08-job-application-agent-peas-design.md`
- `docs/architecture/hello-agents-job-application-agent.md`
- `docs/superpowers/plans/2026-07-08-job-application-agent-mvp.md`
- current repository state in branch `feat/job-agent-next-step`

## Verdict

The repository is runnable and test-green, but the existing design and MVP plan are not safe to use as the direct execution baseline for further implementation.

The correct starting point is:

1. Treat the current codebase as the baseline, not the old greenfield MVP plan.
2. Realign the HelloAgents architecture document to the implementation that actually exists.
3. Create a new implementation plan for the next slice of work instead of resuming the old one.

## Evidence Collected

- Baseline verification in the isolated worktree: `pytest -q`
- Result: `122 passed`
- Current branch for continued work: `feat/job-agent-next-step`

## Findings

### 1. The MVP plan is stale and no longer executable step-by-step

The old plan assumes a mostly empty repository and starts from creating foundational files such as `src/job_agent/config.py`, `src/job_agent/forms.py`, and `src/job_agent/cli.py`.

That is no longer true. The current repository already contains those files, plus additional modules and commands that the plan does not cover, including:

- HelloAgents integration under `src/hello_agents/`
- source-config job ingestion
- shortlist preparation
- batch application packaging
- runtime autofill generation
- profile and sensitive-answer knowledge-base support

If executed literally now, the old plan would duplicate completed work, skip current modules, and produce misleading progress tracking.

### 2. The HelloAgents architecture document overstates the current agent runtime

The architecture document positions `JobApplicationAgent` as a `PlanAndSolveAgent` plus local ReAct-style tool selection.

The current implementation does subclass `PlanAndSolveAgent`, but it overrides `run()` and does not use the planner/executor loop. Instead it deterministically assembles fixed `ToolChain` flows for JD review, resume preparation, and optional form planning.

That means the current system is "HelloAgents-based chain orchestration", not yet the richer agent loop described in the architecture write-up. Continuing implementation without acknowledging this mismatch would cause design drift.

### 3. Config, safety, and message-audit requirements are only partially aligned

The architecture/design docs describe a broader config and audit model, including:

- `JOB_SOURCE_CONFIG_PATH`
- `BROWSER_HEADLESS`
- `AUTO_SUBMIT_ALLOWLIST`
- richer message/audit roles for observation and safety gates

The current `AppConfig` only exposes resume/output/database and LLM settings. The repo does have safety behavior in code, but the documented configuration and audit model is not yet reflected consistently in the implementation.

This is not a reason to stop the project, but it is a reason not to treat the current design doc as implementation-ready without revision.

### 4. Tests prove module-level behavior, not full browser/runtime execution

The current test suite gives useful confidence around:

- CLI workflows
- chain orchestration
- scoring/report generation
- guarded script generation

But the browser-filling pieces are still verified primarily as generated JavaScript text, not as executed Playwright runs against representative forms. That is acceptable for the current stage, but it means "tests green" does not prove the end-to-end application runtime described in the design is already ready.

## Start Conditions For The Next Build Phase

The next implementation phase is real-startable if we follow these rules:

1. Use the current repository state as the source of truth.
2. Do not resume `2026-07-08-job-application-agent-mvp.md` as a live checklist.
3. Use a new plan focused on HelloAgents alignment and truthful scope.
4. Keep the current safety boundaries unchanged:
   - no LinkedIn scraping
   - no automatic final submit
   - no invented profile facts
   - sensitive answers require explicit approval

## Recommended Next Slice

The next slice should focus on making the codebase honestly match the HelloAgents-based design before adding more breadth:

1. Align the agent runtime contract:
   - decide whether `JobApplicationAgent` should be a real plan-and-solve agent or an explicit tool-chain orchestrator
   - update implementation and tests to match that decision
2. Align config and safety policy plumbing:
   - expose the config/policy fields the docs claim exist
   - keep manual-submit and sensitive-field rules authoritative
3. Add explicit audit-trail coverage:
   - verify agent history or structured run-state behavior for review/safety outputs

## Outcome

The project can continue immediately, but only from a corrected plan that matches the current repository and current HelloAgents integration level.
