# Job Application Agent

Local-first personal job application agent built around explicit Perception,
Memory, Agent Core, Policy Gate, Controlled Execution, Tool Use, and verified
Repair layers.

The agent supports compliant job intake, JD review, resume-template selection, fit scoring, and review packet generation. Browser form filling clicks final Submit by default when no blocking review items remain; set `JOB_AGENT_SUBMIT_COMPLETE=0` to force a manual submit gate.

## Daily SOP

Daily operation no longer depends on an agent reconstructing long commands from this README. Start every live application session with:

```bash
.venv/bin/python scripts/daily_sop.py check
.venv/bin/python scripts/daily_sop.py prepare
.venv/bin/python scripts/daily_sop.py execute
.venv/bin/python scripts/daily_sop.py repair --run-dir <run-directory>
.venv/bin/python scripts/daily_sop.py report
.venv/bin/python scripts/daily_sop.py ledger
.venv/bin/python scripts/daily_sop.py cleanup
```

The single local input is `ops/daily.local.json` (created from `ops/daily.example.json` and excluded from git). Every run gets an immutable directory under `output/daily/`, a resumable `run-state.json`, and a concise `RUN_SUMMARY.md`. See `docs/DAILY_APPLICATION_SOP.md` for stage gates, terminal-status handling, and controlled retry rules; coding agents must start from `AGENTS.md` and `docs/PROJECT_MAP.md`.
`execute` continues through complete audited batches until the local-day confirmed-submission target is reached or the candidate pool is empty; `execute --one-batch` is the explicit single-batch mode. The `repair` command resumes a retained scoped request without opening a browser unless `--retry-verified` is explicitly supplied.
When no eligible package is prepared, the run remains in `waiting_for_candidates`, records `next_wake_at` from `empty_wake_minutes`, writes the waiting state to `latest.json`, and exits. A scheduler should start a new `run --execute` cycle at that time; the agent process does not sleep for long periods or report the empty batch as completed.
`output/daily/APPLICATION_LEDGER.csv` is regenerated from SQLite on every SOP command and records confirmed submission time, company, role, status, application URL, and application ID. Canonical application URLs are protected by a database unique key, so tracking-parameter variants, Greenhouse `gh_jid` aliases, and Lever posting/`apply` URL variants reuse the existing record instead of creating a new application.
Each SOP command uses an isolated project-owned workspace under `output/daily/.tmp/` and removes it on exit. The `cleanup` command removes inactive leftovers from interrupted runs without deleting the shared system temporary directory.

## Runtime Components

The `hello_agents` package contains runtime contracts, Agent Core, Simple,
Plan-and-Solve, ReAct and Reflection reasoning strategies, branchable
conversations, policy-controlled ToolChain/async execution, and career tools.
The job application workflow is exposed as:

- `hello_agents.agents.job_application_agent.JobApplicationAgent`
- `hello_agents.tools.builtin.career.ManualJDImportTool`
- `hello_agents.tools.builtin.career.RSSJobSourceTool`
- `hello_agents.tools.builtin.career.GreenhouseJobSourceTool`
- `hello_agents.tools.builtin.career.LeverJobSourceTool`
- `hello_agents.tools.builtin.career.RemotiveJobSourceTool`
- `hello_agents.tools.builtin.career.JDParserTool`
- `hello_agents.tools.builtin.career.FitScorerTool`
- `hello_agents.tools.builtin.career.FormInspectorTool`
- `hello_agents.tools.builtin.career.SensitiveFieldDetectorTool`
- `hello_agents.tools.builtin.career.FormFillerTool`
- `hello_agents.tools.builtin.career.FormFillScriptTool`
- `hello_agents.tools.builtin.career.ResumeIndexerTool`
- `hello_agents.tools.builtin.career.ResumeSelectorTool`
- `hello_agents.tools.builtin.career.ReviewPacketTool`
- `hello_agents.tools.builtin.career.ApplicationTrackerTool`
- `hello_agents.tools.builtin.career.ApplicationPackageTool`
- `hello_agents.tools.builtin.career.SubmitGateTool`

The existing CLI calls the HelloAgents-based `JobApplicationAgent` for JD review.
`JobApplicationAgent.create_reasoning_strategy()` creates a reasoning strategy
that shares the career agent's Policy Gate and ControlledExecution instance.

当前运行架构和完整 Tool 清单见：

- `docs/architecture/runtime-agent-architecture.md`
- `docs/TOOLS.md`

## Safety Boundaries

- No LinkedIn scraping.
- No LinkedIn auto-apply.
- No committed API keys.
- No committed private resume files.
- Browser applications submit automatically by default only when every required field is resolved truthfully and no blocking review item remains.
- Sensitive fields, including sponsorship, work authorization, demographic questions, salary, relocation, and legal attestations, require user review unless explicitly saved.
- A guarded LLM fallback can answer unknown, non-sensitive screening questions from the candidate's own profile facts, but it is never used for sensitive fields, legal attestations, or questions that forbid AI assistance.

### Anthropic Approved Declarations

For Anthropic applications, the candidate has explicitly approved these answers:

- Open to relocation to San Francisco or New York: Yes
- Accepts hybrid or onsite work, including 25% office attendance: Yes
- Acknowledges Anthropic's AI Application Policy: Yes
- Previously interviewed at Anthropic: No

## Hands-free Greenhouse and Workday applications

Two layers now let the runtime finish Greenhouse, Workday, and other ATS applications without stopping for common screening questions:

1. **Screening-answer rules** (`screening_answer_rules` in your profile): write company-agnostic pattern rules once. Example:
   ```json
   "screening_answer_rules": [
     {
       "patterns": [
         "previously worked",
         "currently employed",
         "conflict of interest",
         "contractor",
         "consultant",
         "former employee"
       ],
       "answer": "No"
     }
   ]
   ```
2. **Guarded LLM fallback**: when a non-sensitive question still has no saved answer, the runtime asks the configured LLM to pick an option or write a short answer using only the candidate facts in the profile. It is disabled by default; set `JOB_AGENT_LLM_ANSWERS=1` in `.env` to enable it. `JOB_AGENT_LLM_ANSWERS_MAX_CALLS` bounds per-run API calls (default 40).

When `JOB_AGENT_LLM_ANSWERS=1`, the executor automatically routes to the Python Playwright runtime so the LLM fallback is available. Sensitive fields and legal attestations still require an approved answer from your sensitive-KB file and are never delegated to the LLM.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env`:

```bash
OPENAI_API_KEY=your_key_here
LLM_PROVIDER=openai
LLM_MODEL_ID=gpt-4o-mini
LLM_BASE_URL=https://api.openai.com/v1
RESUME_SOURCE_DIR=/absolute/path/to/your/pdf-resumes
OUTPUT_DIR=output
DATABASE_PATH=job-agent.db
JOB_SOURCE_CONFIG_PATH=/absolute/path/to/sources.json
BROWSER_HEADLESS=true
AUTO_SUBMIT_ALLOWLIST=
CAPMONSTER_API_KEY=
CAPMONSTER_SOLVE_CAPTCHA=false
CAPMONSTER_POLL_INTERVAL_SECONDS=3
CAPMONSTER_TIMEOUT_SECONDS=240
CAPMONSTER_RECAPTCHA_MIN_SCORE=0.3
CAPMONSTER_RECAPTCHA_V2_TASK_TYPE=RecaptchaV2Task
CAPMONSTER_PROXY_TYPE=
CAPMONSTER_PROXY_ADDRESS=
CAPMONSTER_PROXY_PORT=
CAPMONSTER_PROXY_LOGIN=
CAPMONSTER_PROXY_PASSWORD=
JOB_AGENT_SUBMIT_COMPLETE=1
JOB_AGENT_SELF_HEAL_PASSES=3
JOB_AGENT_ANTI_SPAM_COOLDOWN_HOURS=24
JOB_AGENT_ANTI_SPAM_HOST_COOLDOWN_THRESHOLD=5
JOB_AGENT_FAILURE_CIRCUIT_BREAKER_HOURS=6
JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD=2
JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD=
JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_FILE=
JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_STORE=
JOB_AGENT_EMAIL_VERIFICATION_CODE_FILE=
JOB_AGENT_EMAIL_VERIFICATION_WAIT_SECONDS=120
JOB_AGENT_GMAIL_CLIENT_SECRET_FILE=
JOB_AGENT_GMAIL_TOKEN_FILE=
JOB_AGENT_GMAIL_VERIFICATION_QUERY=from:(greenhouse-mail.io) subject:("Security code")
JOB_AGENT_LLM_ANSWERS=1
JOB_AGENT_LLM_ANSWERS_MAX_CALLS=40
```

Set `JOB_AGENT_LLM_ANSWERS=1` to let the runtime answer unknown non-sensitive screening questions with the configured LLM; leave it `0` to keep every unknown question as a blocking review item.

By default, CLI workflows use deterministic local logic. Add `--use-llm` to LLM-aware commands when you want the configured API key/model to be used. You can verify connectivity with:

```bash
job-agent llm smoke --use-llm --prompt "Reply with OK"
```

When `--use-llm` is enabled for review or application preparation commands, the generated packet includes an `LLM Review Notes` section. These notes are advisory only; truthfulness checks and blocking review items still remain authoritative.

`BROWSER_HEADLESS` controls generated runtime browser visibility for verification runs. The runtime clicks final Submit by default only when no blocking review items remain; set `JOB_AGENT_SUBMIT_COMPLETE=0` to restore the manual submit gate.
When `BROWSER_HEADLESS=false` and `JOB_AGENT_SUBMIT_COMPLETE=0`, generated runtime scripts leave the browser open at the submit gate and wait for you to press Enter in the terminal before closing.
Runtime execution first uses Node Playwright when it is installed beside the generated script. If Node Playwright is unavailable, `job-agent applications execute-batch` falls back to the package's Python Playwright runtime and keeps the same submission policy. When `JOB_AGENT_LLM_ANSWERS=1`, the executor routes to the Python Playwright runtime so the guarded LLM fallback can answer unknown screening questions. Install browser binaries once with `python -m playwright install chromium` when setting up a fresh environment.

Before recording a blocker, the runtime re-inspects and refills dynamic fields up to `JOB_AGENT_SELF_HEAL_PASSES` times. A recoverable fill failure is still retried when another field on the page requires an approved or manual answer. Approved sensitive answers take priority, but explicit non-placeholder values already stored directly in the profile can also resolve matching sensitive fields. Unresolved fields are emitted as structured `review_items`, remain the primary outcome, and cannot be relabeled as anti-spam merely because the page also contains a CAPTCHA. For Workday-style candidate account creation, the runtime now auto-generates and reuses a strong local password store at `.job-agent-candidate-passwords.json` by default; override the path with `JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_STORE`, or force a specific password with `JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD` / `JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_FILE`. For email verification, set `JOB_AGENT_EMAIL_VERIFICATION_CODE_FILE` to a file populated by your mail automation; the runtime waits up to `JOB_AGENT_EMAIL_VERIFICATION_WAIT_SECONDS` and accepts only a file write made after the current verification request. Use `JOB_AGENT_EMAIL_VERIFICATION_CODE` only for a deliberate one-off override. The runtime then enters the code, handles any configured CAPTCHA, and resubmits. Submission confirmation is polled before an unconfirmed result is recorded.

For hands-free Gmail verification, install the optional extra with `pip install -e '.[gmail]'`, create a Google OAuth client with the Gmail API enabled, then run `job-agent inbox gmail-authorize --client-secret /path/to/client_secret.json`. You can also set `JOB_AGENT_GMAIL_CLIENT_SECRET_FILE` and run `job-agent inbox gmail-authorize` without repeating the path. The token is stored at `.job-agent-secrets/gmail-token.json` by default, and the runtime uses that path automatically when `JOB_AGENT_GMAIL_TOKEN_FILE` is not set. Set `JOB_AGENT_GMAIL_TOKEN_FILE` to override the token path. The runtime uses only the `gmail.readonly` scope, queries messages newer than the current verification request, and never reads a stale code file. Change `JOB_AGENT_GMAIL_VERIFICATION_QUERY` for a different ATS sender or subject.

Optional CAPTCHA handling uses CapMonster Cloud only when `CAPMONSTER_API_KEY` is set and `CAPMONSTER_SOLVE_CAPTCHA=true`. The runtime calls it only after all blocking review fields have been resolved; when fields still block submission, the audit prints `CapMonster CAPTCHA: skipped (blocking review fields present)` and preserves those fields as the primary outcome. The runtime currently attempts supported reCAPTCHA v2/v3, reCAPTCHA Enterprise, Cloudflare Turnstile, FunCaptcha, GeeTest, and DataDome challenges by creating a CapMonster task, polling until it is ready, and injecting the returned token or cookie into the browser context. After submit, only an explicit CAPTCHA challenge/token error permits one bounded recovery attempt. A `possible spam`, HTTP 429, or server rate-limit response is immediately terminal even when a persistent CAPTCHA element remains in the DOM. `CAPMONSTER_TIMEOUT_SECONDS=240` is recommended for live Greenhouse reCAPTCHA Enterprise submissions, where a solver task can exceed 120 seconds. The default reCAPTCHA v2 and Turnstile task names follow current CapMonster docs; task-type errors retry compatible legacy aliases. DataDome requires the `CAPMONSTER_PROXY_*` settings because CapMonster requires proxy details for that task type. Unsupported solver responses and CAPTCHA recovery failures remain `submission_processing_error`; only explicit server-side spam or rate-limit responses become `submission_blocked_by_anti_spam`. CapMonster cannot remove HTTP 429, server throttling, account restrictions, or unresolved factual fields.

Anti-spam suppression is time-bounded. `JOB_AGENT_ANTI_SPAM_COOLDOWN_HOURS=24` pauses a company when its latest outcome inside that window is an explicit anti-spam block, while a later successful submission clears the company cooldown. A shared apply host is paused only when its recent unresolved blocks reach `JOB_AGENT_ANTI_SPAM_HOST_COOLDOWN_THRESHOLD` (default 5). Greenhouse, Lever, and Ashby hosts are grouped by their company or board tenant so one customer cannot suppress every job on the ATS.

Ordinary runtime failures have a separate circuit breaker. Within `JOB_AGENT_FAILURE_CIRCUIT_BREAKER_HOURS` (default 6), `JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD` consecutive equivalent failures (default 2) pause only the matching company and ATS tenant/adapter. The breaker covers unavailable forms, unresolved autofill blockers, autofill failures/timeouts, and submission processing errors. Limited pipeline batches also take the highest-scored role from each eligible company before using additional roles from a company already represented. A different outcome, a confirmed submission, or window expiry closes the breaker, allowing the pipeline to move to other sources instead of repeating one broken integration.

## Usage

Initialize the local database:

```bash
job-agent init --db job-agent.db
```

Index local resume templates:

```bash
job-agent resumes index "$RESUME_SOURCE_DIR"
```

The indexer scans the configured directory for original PDF resumes only. For every JD, the agent ranks those PDFs by JD skill coverage and role direction, then uploads the best available PDF unchanged. If no PDF has an exact role match, it uploads the closest available PDF rather than generating a new resume.

Import jobs from a compliant public RSS or Atom feed saved as XML:

```bash
job-agent jobs import-rss jobs.xml --out output/jobs.json --source company-careers-rss
```

Import jobs from public Job APIs:

```bash
job-agent jobs import-greenhouse company-board-token --out output/greenhouse-jobs.json
job-agent jobs import-lever company-site-slug --out output/lever-jobs.json
job-agent jobs import-remotive --search "agent engineer" --limit 10 --out output/remotive-jobs.json
```

For offline testing or reproducible runs, each API import command also accepts `--payload path/to/response.json`.

Import or review jobs from a reusable source config:

```json
{
  "sources": [
    {"type": "rss", "source": "company-rss", "rss_file": "jobs.xml"},
    {"type": "greenhouse", "board_token": "company", "limit": 10},
    {"type": "lever", "site": "company", "limit": 10},
    {"type": "remotive", "search": "agent engineer", "limit": 10}
  ]
}
```

```bash
job-agent jobs import-sources sources.json --out output/jobs.json
job-agent jobs shortlist output/jobs.json \
  --min-score 70 \
  --limit 10 \
  --out output/shortlist.json
job-agent jobs review-sources sources.json \
  --out-dir output/source-reviews \
  --resume-source-dir "$RESUME_SOURCE_DIR" \
  --db job-agent.db
```

多来源导入会自动按规范化申请 URL 去重（忽略常见追踪参数）；缺少 URL 时按公司、职位和地点识别重复项。重复岗位会合并来源标记并保留更完整的 JD，避免进入重复匹配和投递队列。

`jobs shortlist` scores the normalized job pool, filters low-fit roles, and writes a ranked JSON file that still contains the standard job fields required by `applications prepare`.

Generate review packets directly from a compliant public RSS or Atom feed:

```bash
job-agent jobs review-rss jobs.xml \
  --out-dir output/rss-reviews \
  --source company-careers-rss \
  --resume-source-dir "$RESUME_SOURCE_DIR" \
  --db job-agent.db
```

Generate review packets directly from public Job APIs:

```bash
job-agent jobs review-greenhouse company-board-token \
  --out-dir output/greenhouse-reviews \
  --resume-source-dir "$RESUME_SOURCE_DIR" \
  --db job-agent.db \
  --use-llm

job-agent jobs review-lever company-site-slug \
  --out-dir output/lever-reviews \
  --resume-source-dir "$RESUME_SOURCE_DIR" \
  --db job-agent.db \
  --use-llm

job-agent jobs review-remotive \
  --search "agent engineer" \
  --limit 10 \
  --out-dir output/remotive-reviews \
  --resume-source-dir "$RESUME_SOURCE_DIR" \
  --db job-agent.db \
  --use-llm
```

Run the complete guarded pipeline from compliant sources to prepared application packages:

```bash
job-agent pipeline run sources.json \
  --out-dir output/pipeline-run \
  --min-score 70 \
  --limit 5 \
  --required-resume-pdf "$RESUME_PDF" \
  --profile examples/profile.json \
  --sensitive-kb examples/sensitive-answers.json \
  --db job-agent.db \
  --use-llm
```

The pipeline imports and deduplicates jobs, ranks them, and emits an `autofill-runtime.js` that uploads the specified PDF unchanged. Use `--required-resume-pdf "$RESUME_PDF"` to force one exact existing PDF path, or `--resume-source-dir "$RESUME_SOURCE_DIR"` when you want the agent to select the closest original PDF from a directory. Browser visibility follows `BROWSER_HEADLESS`; execution uses Node Playwright when present and otherwise falls back to Python Playwright; `--sensitive-kb` carries pre-approved sensitive answers into every generated package; `pipeline-manifest.json` records stage counts, artifact paths, selected PDF paths, and the automatic submit policy.

Run the same flow offline with the included public-data fixture:

```bash
job-agent examples export --out-dir examples

job-agent pipeline run examples/offline-sources.json \
  --out-dir output/offline-pipeline \
  --min-score 0 \
  --profile examples/profile.json \
  --sensitive-kb examples/sensitive-answers.json \
  --db output/offline-pipeline/job-agent.db
```

This fictional fixture is self-contained and exists only to verify the full import -> shortlist -> guarded autofill path without calling a live job API.

For a one-command end-to-end smoke test, including fake runtime execution and audit generation, run:

```bash
job-agent examples verify-offline --out-dir output/offline-verify
```

This exports the packaged fixtures, runs the offline pipeline, injects a fake local Playwright module into each generated package, executes `applications execute-batch`, and writes `output/offline-verify/execution-audit.json`. It does not touch any live job site and is the fastest way to verify that the full local chain is still runnable after changes.

For a real personal setup, scaffold a reusable workspace first:

```bash
job-agent pipeline init-workspace \
  --out-dir my-job-agent \
  --resume my-base-resume.md \
  --job-track "Agent Engineer"
```

This creates `profile.json`, `sensitive-answers.json`, `sources.json`, `resumes/`, `output/`, and `WORKSPACE.md`. `--job-track` adjusts the starter Remotive searches, JD keyword hints, and runbook toward one of the built-in role tracks such as `Agent Engineer`, `ML Infra`, `MLE`, `SDE`, or `Data Scientist`. Review those files, add your role-specific PDF resumes to `resumes/`, replace the starter source entries, and then run the pipeline from that directory.

Prepare a single application package from a normalized `jobs.json` item:

```bash
job-agent applications prepare output/greenhouse-jobs.json \
  --index 1 \
  --out-dir output/acme-agent-engineer \
  --resume-source-dir "$RESUME_SOURCE_DIR" \
  --db job-agent.db \
  --form-snapshot examples/form-snapshot.json \
  --profile examples/profile.json \
  --sensitive-kb examples/sensitive-answers.json \
  --use-llm
```

This writes the review packet, JD analysis, submit gate, and, when source data is provided, a guarded `fill-form.js` script. Providing `--profile` also emits `autofill-runtime.js` for live multi-page ATS inspection and filling. Add `--sensitive-kb` when sensitive answers have been explicitly approved. The agent selects the closest original PDF from `--resume-source-dir` and wires it directly into Resume/CV upload fields; a direct `--resume` value must also be a PDF and is uploaded unchanged.

You can also prepare from a short list:

```bash
job-agent applications prepare output/shortlist.json --index 1 --out-dir output/top-choice
```

Or prepare packages for multiple shortlisted jobs in one batch:

```bash
job-agent applications prepare-shortlist output/shortlist.json \
  --limit 5 \
  --out-dir output/application-batch \
  --required-resume-pdf "$RESUME_PDF" \
  --db job-agent.db \
  --form-snapshot examples/form-snapshot.json \
  --profile examples/profile.json \
  --sensitive-kb examples/sensitive-answers.json \
  --use-llm
```

This creates one subdirectory per job plus `batch-summary.json`, so the user can audit every required or selected PDF path, review packet, approved sensitive-answer merge, and guarded fill script before opening the application pages.

Verify the resume upload evidence before opening any browser:

```bash
job-agent applications verify-resumes output/application-batch/batch-summary.json \
  --out output/application-batch/resume-preflight.json \
  --required-resume-pdf "$RESUME_PDF"
```

The preflight report records each package's upload PDF path, resolved path, existence flag, file size, and SHA256. It exits non-zero if any package would upload a missing, package-local, non-PDF, nonexistent, or non-matching resume.

Generate a guarded runner for the batch fill scripts:

```bash
job-agent applications build-batch-runner output/application-batch/batch-summary.json \
  --out output/application-batch/run-batch.js \
  --required-resume-pdf "$RESUME_PDF"
node output/application-batch/run-batch.js
```

The runner executes each package's `autofill-runtime.js` in sequence, falling back to `fill-form.js` for older per-snapshot packages. Before starting any child runtime, the generated Node runner verifies every upload resume path, PDF extension, package-external location, and required PDF SHA256; if one package fails, no browser runtime is executed. It streams each script's output and accepts runtime terminal markers such as submitted, submit-clicked-unconfirmed, email-verification-required, submission-processing-error, or a blocking submit gate. Terminal input is preserved only when the explicit `JOB_AGENT_SUBMIT_COMPLETE=0` manual mode is used with a headed browser.

Or execute the batch directly and write a privacy-safe audit JSON:

```bash
job-agent applications execute-batch output/application-batch/batch-summary.json \
  --audit-out output/application-batch/execution-audit.json \
  --required-resume-pdf "$RESUME_PDF" \
  --timeout-seconds 300 \
  --llm-answers
```

`execute-batch` refuses to run packages whose upload resume is missing, generated inside the package, non-PDF, nonexistent, or different from `--required-resume-pdf`. It streams each script's output for live review, but the audit file stores only structured status records, exit codes, submit-gate state, the required PDF path, and any parsed review-required field metadata. It does not copy page text, field values, stdout, or stderr into the audit. Every application has an independent timeout and exception boundary; as soon as one reaches a terminal status, the executor atomically updates `execution-audit.json`, reports `Application N/M terminal`, and continues to the next application. The audit's `progress` object exposes planned, terminal, remaining, and complete counts during the run. When a runtime stops on blocking review fields, it also writes `review-required.txt` beside the runtime script so you can see the exact field labels and reasons without replaying the whole run. `--llm-answers` enables the guarded runtime fallback for unknown non-sensitive screening questions for this execution only; pass `--no-llm-answers` or set `JOB_AGENT_LLM_ANSWERS=0` to force every unknown question back into blocking review.

If you want one command from compliant source config all the way to runtime execution and audit generation, use:

```bash
job-agent pipeline run-execute sources.json \
  --out-dir output/pipeline-run \
  --min-score 70 \
  --limit 5 \
  --required-resume-pdf "$RESUME_PDF" \
  --profile examples/profile.json \
  --sensitive-kb examples/sensitive-answers.json \
  --db job-agent.db \
  --timeout-seconds 300 \
  --use-llm
```

This command runs the guarded pipeline, executes each generated runtime script immediately, writes `execution-audit.json`, and updates `pipeline-manifest.json` with both artifact paths and `execution_counts`.
It also writes `resume-preflight.json` before browser execution so each run has a separate proof of the exact PDF path and hash checked before any submit attempt. With `run-execute`, `--use-llm` also enables the guarded runtime LLM fallback unless you add `--no-llm-answers`; the manifest records the effective value as `runtime_llm_answers_enabled`.

Create a review packet from a pasted JD saved as a text file:

```bash
job-agent jobs review jd.txt --out output/application-review.md
```

Create a review packet, select the closest resume template, and write an application tracking record:

```bash
job-agent jobs review jd.txt \
  --out output/application-review.md \
  --resume-source-dir "$RESUME_SOURCE_DIR" \
  --db job-agent.db
```

Create a full local application package with separate review, JD analysis, and submit-gate files:

```bash
job-agent jobs review jd.txt \
  --out output/application-review.md \
  --resume-source-dir "$RESUME_SOURCE_DIR" \
  --db job-agent.db \
  --package-dir output/acme-agent-engineer
```

Create a guarded form-fill plan from a captured form snapshot and approved profile facts:

```bash
job-agent forms build-snapshot-script \
  --application-url "https://example.com/apply" \
  --snapshot-out form-snapshot.json \
  --out output/capture-form-snapshot.js
```

The snapshot script only reads form metadata such as labels, field types, required flags, and select options. It does not fill fields, upload files, click buttons, or submit the application.

```bash
job-agent jobs review jd.txt \
  --out output/application-review.md \
  --form-snapshot examples/form-snapshot.json \
  --profile examples/profile.json \
  --sensitive-kb examples/sensitive-answers.json
```

The form-fill plan maps low-risk fields such as email, name, phone, LinkedIn, GitHub, portfolio, website, location, cover letter, and approved resume uploads. For company-specific questions, add exact label matches under `answers` in `examples/profile.json`; select fields use the approved answer as the visible option label. Sensitive fields such as sponsorship, work authorization, salary, relocation, demographic, disability, veteran, and legal-attestation fields stay review-required when they appear only in ordinary `answers`; pass an approved `--sensitive-kb` file to fill them automatically. The live runtime clicks Submit automatically when no blocking review items remain.

Generate a guarded per-snapshot Playwright fill script. This static helper does not submit; use `forms autofill` or an application package's `autofill-runtime.js` for the default guarded automatic-submit flow:

```bash
job-agent forms build-script \
  --form-snapshot examples/form-snapshot.json \
  --profile examples/profile.json \
  --sensitive-kb examples/sensitive-answers.json \
  --resume-file "$RESUME_SOURCE_DIR/your-resume.pdf" \
  --application-url "https://example.com/apply" \
  --out output/fill-form.js
```

Use the HelloAgents API directly:

```python
from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.core.llm import HelloAgentsLLM


llm = HelloAgentsLLM(provider="openai", model="gpt-4o-mini")
agent = JobApplicationAgent(name="career-agent", llm=llm)
print(agent.run("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents."))
```

## Development

Run tests with external pytest plugin autoload disabled if your global Python environment has unrelated plugins:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -v
```

## Current Capabilities

- Environment-based configuration.
- SQLite schema for jobs, resume templates, fit scores, applications, and generated documents.
- Original PDF resume indexing and JD-based PDF selection for upload.
- Manual JD import from text.
- Public RSS/Atom job feed import with normalized source/apply URLs.
- Public Greenhouse, Lever, and Remotive job API imports with normalized source/apply URLs.
- Configurable multi-source job import and batch review from `sources.json`.
- Fit-score shortlisting for normalized job pools before PDF resume matching or application preparation.
- Batch review-packet generation from RSS/Atom, Greenhouse, Lever, and Remotive job source items.
- Single-job application package preparation from normalized job source JSON.
- Batch application package preparation from shortlisted job JSON.
- Guarded batch runner generation for sequential low-risk form filling.
- Expanded low-risk profile mapping for portfolio, website, location, and cover-letter fields.
- Approved exact-label answers for custom ATS questions and select fields.
- JD-based matching and unchanged upload of the closest original PDF resume.
- Structured JD analysis with role track, skills, responsibilities, and risks.
- Deterministic role classification and explainable fit scoring.
- Markdown application review packet generation.
- HelloAgents-based resume selection and application tracking tools.
- Local application package export for review artifacts.
- Guarded Playwright script generation for form snapshot capture.
- Form snapshot inspection, sensitive-field detection, and guarded form-fill planning.
- Guarded Playwright script generation for low-risk browser form filling and approved Resume/CV file upload.
- Guarded form-fill plan model with automatic final submission when no blocking review items remain.
