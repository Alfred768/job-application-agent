# Job Application Agent

Local-first personal job application agent designed with a PEAS task-environment model.

The MVP helps with compliant job intake, JD review, resume-template selection, fit scoring, and review packet generation. Browser form filling is designed with a hard safety gate: final Submit remains manual unless a source-specific adapter explicitly permits auto-submit.

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

Create a review packet from a pasted JD saved as a text file:

```bash
job-agent jobs review jd.txt --out output/application-review.md
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
- Deterministic role classification and explainable fit scoring.
- Markdown application review packet generation.
- Guarded form-fill plan model with manual final-submit policy.
