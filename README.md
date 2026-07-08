# Job Application Agent

Local-first personal job application agent designed with a PEAS task-environment model and built on a HelloAgents-style agent/tool framework.

The MVP helps with compliant job intake, JD review, resume-template selection, fit scoring, and review packet generation. Browser form filling is designed with a hard safety gate: final Submit remains manual unless a source-specific adapter explicitly permits auto-submit.

## HelloAgents Base

This project now includes a `hello_agents` package adapted from the public Datawhale Hello-Agents ecosystem, specifically the reusable `hello_agents` framework package found under the upstream co-creation projects. The job application workflow is exposed as:

- `hello_agents.agents.job_application_agent.JobApplicationAgent`
- `hello_agents.tools.builtin.career.ManualJDImportTool`
- `hello_agents.tools.builtin.career.RSSJobSourceTool`
- `hello_agents.tools.builtin.career.JDParserTool`
- `hello_agents.tools.builtin.career.FitScorerTool`
- `hello_agents.tools.builtin.career.FormInspectorTool`
- `hello_agents.tools.builtin.career.SensitiveFieldDetectorTool`
- `hello_agents.tools.builtin.career.FormFillerTool`
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
RESUME_SOURCE_DIR=/absolute/path/to/your/english-resumes
OUTPUT_DIR=output
DATABASE_PATH=job-agent.db
```

## Usage

Initialize the local database:

```bash
job-agent init --db job-agent.db
```

Index local resume templates:

```bash
job-agent resumes index "$RESUME_SOURCE_DIR"
```

Import jobs from a compliant public RSS or Atom feed saved as XML:

```bash
job-agent jobs import-rss jobs.xml --out output/jobs.json --source company-careers-rss
```

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
job-agent jobs review jd.txt \
  --out output/application-review.md \
  --form-snapshot examples/form-snapshot.json \
  --profile examples/profile.json
```

The form-fill plan maps low-risk fields such as email/name and marks sensitive fields such as sponsorship, work authorization, salary, relocation, demographic, disability, veteran, and legal-attestation fields for review. It does not click Submit.

Use the HelloAgents API directly:

```python
from hello_agents.agents.job_application_agent import JobApplicationAgent


class DeterministicLLM:
    provider = "deterministic"

    def invoke(self, messages, **kwargs):
        return ""


agent = JobApplicationAgent(name="career-agent", llm=DeterministicLLM())
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
- Structured JD analysis with role track, skills, responsibilities, and risks.
- Deterministic role classification and explainable fit scoring.
- Markdown application review packet generation.
- HelloAgents-based resume selection and application tracking tools.
- Auditable resume edit plan generation with unsupported keyword detection.
- Local application package export for review artifacts.
- Form snapshot inspection, sensitive-field detection, and guarded form-fill planning.
- Guarded form-fill plan model with manual final-submit policy.
