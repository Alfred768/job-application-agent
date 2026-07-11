# Job Application Agent HelloAgents Alignment Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing job application agent honestly match its HelloAgents-based design by aligning agent orchestration, configuration policy, and audit-trail behavior with the current codebase.

**Architecture:** Treat the current repository as the baseline system. Tighten the contract between `hello_agents` orchestration and `job_agent` CLI/services instead of pretending the project is still at the greenfield MVP stage. Preserve existing safety gates while making the actual agent runtime and documented design agree.

**Tech Stack:** Python 3.9+, Typer, Pydantic, SQLite, pytest, HelloAgents local framework, Playwright-ready script generation.

---

## Chunk 1: Freeze The Correct Baseline

### Task 1: Mark Old Docs As Non-Executable Baselines

**Files:**
- Modify: `docs/superpowers/specs/2026-07-08-job-application-agent-peas-design.md`
- Modify: `docs/superpowers/plans/2026-07-08-job-application-agent-mvp.md`
- Create: `docs/superpowers/specs/2026-07-09-job-application-agent-execution-readiness-audit.md`

- [ ] **Step 1: Write the audit/update docs**

Record that the old design/plan need alignment review and that the 2026-07-08 MVP plan is no longer a live execution checklist.

- [ ] **Step 2: Verify the docs reference current repository reality**

Run: `sed -n '1,120p' docs/superpowers/specs/2026-07-09-job-application-agent-execution-readiness-audit.md`
Expected: shows stale-plan finding, HelloAgents runtime mismatch, and next-slice recommendation.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-08-job-application-agent-peas-design.md \
  docs/superpowers/plans/2026-07-08-job-application-agent-mvp.md \
  docs/superpowers/specs/2026-07-09-job-application-agent-execution-readiness-audit.md
git commit -m "docs: audit execution readiness for job application agent"
```

## Chunk 2: Align The Agent Runtime Contract

### Task 2: Add a Failing Test For The Intended JobApplicationAgent Contract

**Files:**
- Create: `tests/test_job_application_agent_contract.py`
- Modify: `src/hello_agents/agents/job_application_agent.py`
- Test: `tests/test_job_application_agent_contract.py`

- [ ] **Step 1: Write the failing test**

Write one focused test that locks down the desired runtime contract. Pick one of these and commit to it in the test:

- `JobApplicationAgent` is a deterministic chain orchestrator and should record review/safety outputs into agent history/state.
- or `JobApplicationAgent` must actually use the inherited plan-and-solve planner/executor flow.

Do not implement both directions at once.

- [ ] **Step 2: Run the test and verify it fails for the expected reason**

Run: `pytest tests/test_job_application_agent_contract.py -v`
Expected: FAIL because the current runtime contract is ambiguous or missing the asserted behavior.

- [ ] **Step 3: Implement the minimal alignment**

If choosing the chain-orchestrator direction:
- make the class contract explicit in code and docs
- record run outputs in agent history or structured state

If choosing the real plan-and-solve direction:
- route execution through planner/executor
- keep tool use and submit-gate behavior testable and auditable

- [ ] **Step 4: Re-run the targeted test**

Run: `pytest tests/test_job_application_agent_contract.py -v`
Expected: PASS.

- [ ] **Step 5: Run nearby regression tests**

Run: `pytest tests/test_hello_agents_base.py tests/test_cli.py tests/test_cli_llm.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_job_application_agent_contract.py src/hello_agents/agents/job_application_agent.py
git commit -m "feat: align job application agent runtime contract"
```

## Chunk 3: Align Config And Safety Policy

### Task 3: Add the Missing Policy Config Surface

**Files:**
- Modify: `src/job_agent/config.py`
- Modify: `tests/test_config.py`
- Modify: `README.md`

- [ ] **Step 1: Write the failing tests**

Add focused tests for the config fields the docs already claim exist, at minimum:
- `BROWSER_HEADLESS`
- `AUTO_SUBMIT_ALLOWLIST`
- optional `JOB_SOURCE_CONFIG_PATH`

- [ ] **Step 2: Run the targeted tests and verify they fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL because the current config model does not expose the new fields.

- [ ] **Step 3: Implement the minimal config additions**

Expose the missing fields in `AppConfig`, keeping safe defaults:
- browser defaults to headless or explicit CLI override
- auto-submit allowlist defaults empty
- source-config path stays optional

- [ ] **Step 4: Re-run the targeted tests**

Run: `pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Update README**

Document the new config fields without implying unsafe auto-submit is enabled by default.

- [ ] **Step 6: Run focused regression**

Run: `pytest tests/test_config.py tests/test_cli.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/job_agent/config.py tests/test_config.py README.md
git commit -m "feat: align job agent policy configuration"
```

## Chunk 4: Add Explicit Audit-Trail Coverage

### Task 4: Lock Down Safety/Audit Outputs In Tests

**Files:**
- Modify: `tests/test_hello_agents_base.py`
- Modify: `tests/test_cli.py`
- Modify: `src/hello_agents/agents/job_application_agent.py`

- [ ] **Step 1: Write one failing test for audit visibility**

Examples:
- review output always includes submit-gate section
- safety outputs are appended to history/state
- form-planning output surfaces review-required sensitive fields

- [ ] **Step 2: Run the failing test**

Run: `pytest tests/test_hello_agents_base.py -v`
Expected: FAIL for the new audit-visibility behavior.

- [ ] **Step 3: Implement the minimum code**

Keep the implementation small. Do not expand browser capability here; only make the existing safety/audit contract explicit and testable.

- [ ] **Step 4: Run focused regression**

Run: `pytest tests/test_hello_agents_base.py tests/test_cli.py tests/test_cli_llm.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_hello_agents_base.py tests/test_cli.py src/hello_agents/agents/job_application_agent.py
git commit -m "test: lock down job agent audit trail behavior"
```

## Chunk 5: Full Verification

### Task 5: Prove The Alignment Slice Is Stable

**Files:**
- No new files required.

- [ ] **Step 1: Run the full suite**

Run: `pytest -q`
Expected: PASS with no failures.

- [ ] **Step 2: Review changed files**

Run: `git diff --stat HEAD~4..HEAD`
Expected: only alignment, config, docs, and test files changed for this slice.

- [ ] **Step 3: Prepare for branch-finishing workflow**

Use `superpowers:finishing-a-development-branch` after verification is green.
