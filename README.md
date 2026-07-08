# Job Application Agent

Local-first personal job application agent designed with a PEAS task-environment model and built on a HelloAgents-style agent/tool framework.

The MVP helps with compliant job intake, JD review, resume-template selection, fit scoring, and review packet generation. Browser form filling is designed with a hard safety gate: final Submit remains manual unless a source-specific adapter explicitly permits auto-submit.

## HelloAgents Base

This project now includes a `hello_agents` package adapted from the public Datawhale Hello-Agents ecosystem, specifically the reusable `hello_agents` framework package found under the upstream co-creation projects. The job application workflow is exposed as:

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
- `hello_agents.tools.builtin.career.ResumeTailorTool`
- `hello_agents.tools.builtin.career.TruthfulnessCheckTool`
- `hello_agents.tools.builtin.career.ReviewPacketTool`
- `hello_agents.tools.builtin.career.ApplicationTrackerTool`
- `hello_agents.tools.builtin.career.ApplicationPackageTool`
- `hello_agents.tools.builtin.career.SubmitGateTool`

The existing CLI calls the HelloAgents-based `JobApplicationAgent` for JD review.

## Safety Boundaries

- No LinkedIn scraping.
- No LinkedIn auto-apply.
- No committed API keys.
- No committed private resume files.
- No automatic final Submit for ordinary browser-based applications.
- Sensitive fields, including sponsorship, work authorization, demographic questions, salary, relocation, and legal attestations, require user review unless explicitly saved.

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
RESUME_SOURCE_DIR=/absolute/path/to/your/english-resumes
OUTPUT_DIR=output
DATABASE_PATH=job-agent.db
```

By default, CLI workflows use deterministic local logic. Add `--use-llm` to LLM-aware commands when you want the configured API key/model to be used. You can verify connectivity with:

```bash
job-agent llm smoke --use-llm --prompt "Reply with OK"
```

When `--use-llm` is enabled for review or application preparation commands, the generated packet includes an `LLM Review Notes` section. These notes are advisory only; the truthfulness gate and manual Submit gate still remain authoritative.

## Usage

Initialize the local database:

```bash
job-agent init --db job-agent.db
```

Index local resume templates:

```bash
job-agent resumes index "$RESUME_SOURCE_DIR"
```

Generate a grounded tailored resume draft from a JD and base resume text:

```bash
job-agent resumes tailor jd.txt \
  --resume resume.txt \
  --out output/tailored-resume.md
```

The tailored draft preserves the base resume text, emphasizes supported JD keywords, and lists unsupported keywords for manual review instead of inserting them as claims.

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

Prepare a single application package from a normalized `jobs.json` item:

```bash
job-agent applications prepare output/greenhouse-jobs.json \
  --index 1 \
  --out-dir output/acme-agent-engineer \
  --resume-source-dir "$RESUME_SOURCE_DIR" \
  --db job-agent.db \
  --form-snapshot examples/form-snapshot.json \
  --profile examples/profile.json \
  --resume resume.txt \
  --upload-resume \
  --use-llm
```

This writes the review packet, JD analysis, resume edit plan, submit gate, and, when source data is provided, a guarded `fill-form.js` script plus `tailored-resume.md`. With `--upload-resume`, the script wires `tailored-resume.md` into Resume/CV upload fields, but it still does not click Submit.

You can also prepare from a short list:

```bash
job-agent applications prepare output/shortlist.json --index 1 --out-dir output/top-choice
```

Or prepare packages for multiple shortlisted jobs in one batch:

```bash
job-agent applications prepare-shortlist output/shortlist.json \
  --limit 5 \
  --out-dir output/application-batch \
  --resume-source-dir "$RESUME_SOURCE_DIR" \
  --db job-agent.db \
  --form-snapshot examples/form-snapshot.json \
  --profile examples/profile.json \
  --resume resume.txt \
  --upload-resume \
  --use-llm
```

This creates one subdirectory per job plus `batch-summary.json`, so the user can audit every generated review packet, tailored resume draft, and guarded fill script before opening the application pages.

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

Create a full local application package with separate review, JD analysis, resume edit plan, and submit-gate files:

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
  --profile examples/profile.json
```

The form-fill plan maps low-risk fields such as email/name and marks sensitive fields such as sponsorship, work authorization, salary, relocation, demographic, disability, veteran, and legal-attestation fields for review. It does not click Submit.

Generate a guarded Playwright script that fills only low-risk fields and pauses before final submission:

```bash
job-agent forms build-script \
  --form-snapshot examples/form-snapshot.json \
  --profile examples/profile.json \
  --resume-file output/tailored-resume.pdf \
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

## Current MVP Capabilities

- Environment-based configuration.
- SQLite schema for jobs, resume templates, fit scores, applications, and generated documents.
- Resume template indexing for role-specific DOCX/PDF files.
- Manual JD import from text.
- Public RSS/Atom job feed import with normalized source/apply URLs.
- Public Greenhouse, Lever, and Remotive job API imports with normalized source/apply URLs.
- Configurable multi-source job import and batch review from `sources.json`.
- Fit-score shortlisting for normalized job pools before resume tailoring or application preparation.
- Batch review-packet generation from RSS/Atom, Greenhouse, Lever, and Remotive job source items.
- Single-job application package preparation from normalized job source JSON.
- Batch application package preparation from shortlisted job JSON.
- Structured JD analysis with role track, skills, responsibilities, and risks.
- Deterministic role classification and explainable fit scoring.
- Markdown application review packet generation.
- HelloAgents-based resume selection and application tracking tools.
- Auditable resume edit plan generation with unsupported keyword detection.
- Grounded tailored resume draft generation that does not overwrite source resumes.
- Local application package export for review artifacts.
- Guarded Playwright script generation for form snapshot capture.
- Form snapshot inspection, sensitive-field detection, and guarded form-fill planning.
- Guarded Playwright script generation for low-risk browser form filling and approved Resume/CV file upload.
- Guarded form-fill plan model with manual final-submit policy.
