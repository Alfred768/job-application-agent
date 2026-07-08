# Personal Job Application Agent PEAS Design

Date: 2026-07-08
Status: Ready for user review
Project root: `/Users/wugaoyi/Learning/Project/Agent_Lesson/Application`
Resume source: `/Users/wugaoyi/Learning/求职/英文简历(最新)`

## 1. Project Goal

Build a personal job application agent that helps Gaoyi Wu find relevant jobs from compliant public sources, evaluate fit, tailor resume materials, prepare application answers, and fill application forms. The MVP must keep the user in control: the agent may fill forms and upload files, but it pauses before the final submission unless a target source explicitly allows automated submission through an official API or documented workflow.

The agent is not a LinkedIn scraping bot. LinkedIn automated scraping and website automation are outside the MVP boundary because LinkedIn's official materials prohibit unauthorized automated access and third-party automation on its website. LinkedIn jobs can be handled only through user-provided links/JD text or other compliant sources.

## 2. PEAS Task Environment

### 2.1 Performance Measures

The agent optimizes for application quality, user control, traceability, and compliant automation.

Primary performance measures:

- Fit quality: the job matches the user's target roles, level, location, work authorization constraints, and experience.
- Resume relevance: the generated resume preserves truthful experience while improving JD keyword coverage, skills ordering, and ATS readability.
- Application completeness: required fields, documents, links, and answers are filled correctly.
- Truthfulness: the agent must not invent companies, degrees, publications, metrics, employment history, citizenship status, work authorization, or personal facts.
- Compliance: the agent must not bypass site rules, scrape prohibited sites, evade bot detection, or submit applications where automated submission is not permitted.
- Human control: final submit requires user confirmation for ordinary browser-based applications.
- Efficiency: the agent reduces repetitive work in job discovery, JD parsing, resume tailoring, cover letter drafting, and form filling.
- Deduplication: the agent avoids reapplying to the same company/role unless the user explicitly approves.
- Observability: every job, score, generated document, decision, form-fill attempt, and user confirmation is logged.

Secondary performance measures:

- Time from JD discovery to ready-to-review application.
- Percentage of generated resumes that fit on one page after rendering.
- Form-fill success rate by ATS provider.
- Number of user corrections needed per application.
- Interview callback rate, once outcome data exists.

### 2.2 Environment

The environment contains external job sources, local user assets, LLM services, browser/ATS pages, and the user's decisions.

Job sources:

- Public job APIs such as Adzuna, Remotive, Arbeitnow, USAJobs, or other documented job APIs.
- Company ATS feeds and boards where public access is intended, especially Greenhouse and Lever job board endpoints.
- RSS feeds and public company career pages where crawling is allowed by site rules.
- User-provided JD text or URLs.

User assets:

- Existing resume templates in `/Users/wugaoyi/Learning/求职/英文简历(最新)`.
- Existing role-oriented templates: Agent Engineer, SDE, MLE, ML Infra, AI Algorithm Engineer, Data Scientist, Unity ML Infrastructure.
- User profile facts: name, contact details, links, education, work history, publications, projects, work authorization answers, preferences, and reusable application answers.

Application surfaces:

- ATS pages such as Greenhouse, Lever, Ashby, Workday, SmartRecruiters, and company-specific forms.
- File upload controls for resume and cover letter.
- Form fields for personal details, links, demographic questions, authorization questions, and custom screening questions.

Agent infrastructure:

- LLM API for JD parsing, fit scoring, resume planning, bullet editing, cover letter generation, and form-answer drafting.
- Local database or structured files for jobs, applications, documents, decisions, and logs.
- Browser automation runtime for filling forms.
- DOCX/PDF generation and rendering pipeline for tailored resumes.

### 2.3 Actuators

Actuators are the actions the agent can take to change the environment.

Job discovery actuators:

- Query configured job APIs.
- Read configured RSS feeds.
- Fetch public ATS job listings from allowed endpoints.
- Import user-provided JD text or URLs.

Analysis actuators:

- Parse JD fields: title, company, location, remote policy, salary if present, required skills, preferred skills, responsibilities, experience level, ATS URL, and source metadata.
- Score job fit against the user profile and preferences.
- Classify job track: Agent Engineer, SDE, MLE, ML Infra, AI Algorithm Engineer, Data Scientist, Unity ML Infrastructure, or Other.
- Detect duplicate jobs and previously applied roles.

Document actuators:

- Select the best base resume template.
- Produce a resume edit plan before editing.
- Rewrite only safe sections: summary, skills ordering, selected bullets, project emphasis, and optional cover letter.
- Generate DOCX and PDF outputs.
- Render and inspect the resume output for one-page fit and formatting defects.

Application actuators:

- Open the application URL in a controlled browser session.
- Detect form fields and required fields.
- Fill standard profile fields.
- Upload the generated resume PDF and optional cover letter.
- Draft answers for screening questions.
- Pause before final submission and show a review checklist.
- Record the user's final action: submitted, skipped, needs manual work, or failed.

Notification and tracking actuators:

- Save job/application status.
- Create reminders for follow-up.
- Export a CSV/Markdown application log.
- Surface failures and fields requiring user review.

### 2.4 Sensors

Sensors are the inputs the agent reads from the environment.

Job sensors:

- API/RSS responses.
- Public ATS job JSON/HTML where permitted.
- User-provided JD text.
- Job page metadata.

Resume/profile sensors:

- Existing DOCX/PDF resume templates.
- Parsed resume text and section structure.
- User preference configuration.
- Reusable answer bank.

Browser/form sensors:

- DOM field labels, placeholder text, required attributes, input types, select options, radio groups, upload controls, and validation messages.
- Current browser URL and page state.
- Upload success/failure signals.
- Submit button presence and disabled/enabled state.

Quality sensors:

- LLM self-check output.
- Keyword coverage comparison between JD and tailored resume.
- Truthfulness check against user profile facts.
- PDF render check and page count.
- Application history records.

User sensors:

- User approval/rejection of jobs.
- User edits to generated materials.
- User confirmation before submit.
- User corrections to profile facts or screening answers.

## 3. Agent Personality and Decision Policy

The agent should behave like a careful personal career operations assistant:

- Ambitious but not spammy.
- Precise but not rigid.
- Truthful by default.
- Respectful of platform rules.
- Optimized for high-quality applications rather than mass application volume.
- Transparent about why a job was selected, skipped, or marked risky.

Decision policy:

- If a job is low fit, the agent should skip or ask before spending time on materials.
- If a required fact is unknown, the agent must ask or mark the field for review.
- If a website prohibits automation, the agent must not automate it.
- If an answer affects legal status, work authorization, disability, veteran status, demographic information, salary expectations, or relocation commitments, the agent should either use a user-approved saved answer or pause for review.
- If final submission is browser-based, the agent must pause before clicking Submit.

## 4. Architecture Overview

The system should be built as a modular local-first agent application.

Recommended MVP architecture:

- CLI or lightweight local web app as the operator interface.
- SQLite database for jobs, applications, generated documents, and audit logs.
- Python backend for job ingestion, parsing, scoring, document generation, and orchestration.
- Browser automation layer for ATS form filling.
- LLM provider adapter for OpenAI-compatible APIs.
- Document pipeline that starts from DOCX templates and exports PDF.

High-level flow:

1. Ingest jobs from allowed sources.
2. Normalize each job into a common job schema.
3. Score and classify each job.
4. Choose a base resume template.
5. Generate a resume edit plan and validate it against user facts.
6. Create tailored DOCX/PDF resume.
7. Optionally draft cover letter and screening answers.
8. Open application page and fill the form.
9. Pause before final submit.
10. Record outcome and next steps.

## 5. Core Modules

### 5.1 Source Connectors

Purpose: retrieve jobs from compliant sources and normalize them.

Initial connectors:

- `ManualJDConnector`: imports pasted JD text or a user-provided URL.
- `RSSConnector`: reads configured RSS feeds.
- `GreenhouseConnector`: reads public Greenhouse job board endpoints when available.
- `LeverConnector`: reads public Lever postings endpoints when available.
- `JobAPIConnector`: wraps configured APIs such as Remotive or Adzuna.

Each connector should return:

- Source name.
- Source URL.
- Job title.
- Company.
- Location.
- Remote policy if known.
- JD text.
- Apply URL.
- Retrieved timestamp.

### 5.2 JD Parser

Purpose: convert raw JD text into structured requirements.

Outputs:

- Role title and normalized role family.
- Seniority estimate.
- Required skills.
- Preferred skills.
- Responsibilities.
- Domain keywords.
- Location/remote constraints.
- Work authorization hints.
- ATS provider if detectable.
- Red flags such as unpaid roles, scams, unrealistic requirements, or incompatible location.

Tool: LLM API with structured JSON output plus deterministic post-validation.

### 5.3 Fit Scorer

Purpose: rank jobs before spending effort.

Scoring dimensions:

- Role-family match.
- Technical skill match.
- Experience-level match.
- Location/remote match.
- Domain match.
- Resume evidence strength.
- Application complexity.
- Risk/compliance flags.

The score should be explainable. Example:

- `fit_score`: 84
- `recommended_track`: `Agent Engineer`
- `reasons`: `["Strong LLM orchestration match", "Kafka/FastAPI requested", "New grad acceptable"]`
- `missing_keywords`: `["LangGraph", "evaluation pipelines"]`

### 5.4 Resume Template Selector

Purpose: choose the best starting resume from existing files.

Available templates:

- `GAOYI_WU_Agent_Engineer.docx`
- `GAOYI_WU_SDE.docx`
- `GAOYI_WU_MLE.docx`
- `GAOYI_WU_ML_Infra.docx`
- `GAOYI_WU_AI_Algorithm_Engineer.docx`
- `GAOYI_WU_Data_Scientist.docx`
- `GAOYI_WU_Unity_ML_Infrastructure.docx`

MVP strategy:

- Use the existing seven templates as role-specific bases.
- Do not merge them into one master resume yet.
- Select the closest template from the JD classification.
- Customize lightly and preserve one-page formatting.

### 5.5 Resume Tailor

Purpose: produce a targeted resume while preserving truthfulness.

Allowed edits:

- Rewrite summary toward the target role.
- Reorder skills and add only skills already supported by resume/profile evidence.
- Rephrase bullets to surface JD keywords already supported by real projects.
- Swap emphasis among existing projects when page budget requires it.
- Adjust section labels only if needed for ATS clarity.

Forbidden edits:

- Inventing new experience, employers, degrees, publications, certifications, metrics, tools, or dates.
- Changing contact details without user approval.
- Adding keywords with no factual support.
- Making claims about citizenship, clearance, work authorization, or location commitments without saved user approval.

Tools:

- DOCX parser/editor for template-preserving edits.
- LLM API for edit proposals.
- Truthfulness checker comparing proposed edits to source facts.
- DOCX-to-PDF renderer and visual/page-count check.

### 5.6 Cover Letter and Screening Answer Generator

Purpose: draft optional application materials.

Inputs:

- JD summary.
- Company.
- Selected resume evidence.
- User tone preference.
- Saved answer bank.

Outputs:

- Short cover letter.
- Why this company answer.
- Why this role answer.
- Project/experience short answers.

Rules:

- Mark uncertain facts as `needs_user_review`.
- Keep answers concise for form fields.
- Avoid generic overclaiming.

### 5.7 Form Filler

Purpose: fill browser-based applications while preserving user control.

Tools:

- Browser automation tool such as Playwright.
- Field-mapping model that maps visible labels to profile fields.
- File upload handler.
- Validation reader for required-field errors.

MVP form-fill policy:

- Fill standard fields automatically.
- Upload tailored resume automatically.
- Fill saved, low-risk answers automatically.
- Pause for sensitive or uncertain fields.
- Stop before final Submit.

Sensitive fields include:

- Work authorization.
- Sponsorship.
- EEO/demographic questions.
- Disability/veteran status.
- Salary expectations.
- Relocation.
- Start date.
- Legal attestations.

### 5.8 Application Tracker

Purpose: keep the agent stateful and auditable.

Tracks:

- Job source and retrieved timestamp.
- Fit score and reasons.
- Resume template selected.
- Generated files.
- Form-fill status.
- User final action.
- Submission date if user submits.
- Follow-up reminders.
- Outcome: rejected, interview, offer, ghosted, withdrawn.

Recommended MVP storage:

- SQLite database for structured state.
- Local file folders for generated documents.
- Markdown or CSV export for human review.

## 6. Tool Inventory

The agent should expose tools through a narrow interface rather than letting the LLM freely operate the system.

Job tools:

- `search_jobs(source, query, location, remote, limit)`
- `import_job_from_url(url)`
- `import_job_from_text(text)`
- `normalize_job(raw_job)`

Analysis tools:

- `parse_jd(job_id)`
- `score_fit(job_id, profile_id)`
- `classify_role(job_id)`
- `detect_duplicate(job_id)`

Resume tools:

- `list_resume_templates()`
- `extract_resume_text(template_id)`
- `select_resume_template(job_id)`
- `propose_resume_edits(job_id, template_id)`
- `validate_resume_edits(edit_plan_id)`
- `render_tailored_resume(edit_plan_id)`
- `export_resume_pdf(document_id)`

Application tools:

- `open_application(apply_url)`
- `inspect_form()`
- `map_form_fields(form_snapshot, profile_id)`
- `fill_form(mapped_fields)`
- `upload_file(field_id, file_path)`
- `validate_required_fields()`
- `pause_for_user_review(reason)`

Tracker tools:

- `create_application_record(job_id)`
- `update_application_status(application_id, status)`
- `attach_document(application_id, document_id)`
- `log_decision(application_id, decision, reason)`
- `export_applications(format)`

Safety tools:

- `check_source_policy(source_url)`
- `detect_sensitive_fields(form_snapshot)`
- `truthfulness_check(generated_text, source_facts)`
- `compliance_gate(action)`

## 7. Data Model

Core tables or JSON collections:

### 7.1 `jobs`

- `id`
- `source`
- `source_url`
- `apply_url`
- `title`
- `company`
- `location`
- `remote_policy`
- `raw_jd`
- `parsed_jd_json`
- `retrieved_at`
- `status`

### 7.2 `fit_scores`

- `job_id`
- `score`
- `role_track`
- `matched_skills`
- `missing_keywords`
- `risks`
- `recommendation`
- `explanation`

### 7.3 `resume_templates`

- `id`
- `track`
- `docx_path`
- `pdf_path`
- `parsed_text`
- `last_indexed_at`

### 7.4 `generated_documents`

- `id`
- `job_id`
- `template_id`
- `docx_path`
- `pdf_path`
- `edit_plan_json`
- `quality_checks_json`
- `created_at`

### 7.5 `applications`

- `id`
- `job_id`
- `company`
- `title`
- `apply_url`
- `status`
- `generated_resume_id`
- `cover_letter_id`
- `form_snapshot_json`
- `user_review_notes`
- `submitted_at`
- `updated_at`

### 7.6 `profile_facts`

- `field`
- `value`
- `sensitivity`
- `source`
- `last_confirmed_at`

### 7.7 `answer_bank`

- `question_pattern`
- `answer`
- `sensitivity`
- `requires_review`
- `last_confirmed_at`

## 8. Application Workflow

### 8.1 Discovery

1. User sets target query, location, remote preference, and seniority.
2. Agent queries allowed sources.
3. Agent normalizes jobs.
4. Agent removes duplicates.
5. Agent scores jobs and shows ranked recommendations.

### 8.2 Material Generation

1. User approves one or more jobs for preparation.
2. Agent selects a base resume template.
3. Agent creates a resume edit plan.
4. Agent validates edits against source facts.
5. Agent generates tailored DOCX/PDF.
6. Agent checks page count and formatting.
7. Agent drafts optional cover letter and screening answers.

### 8.3 Form Fill

1. Agent opens the application page.
2. Agent inspects form fields.
3. Agent maps fields to profile facts or generated answers.
4. Agent fills non-sensitive fields.
5. Agent uploads generated resume.
6. Agent pauses for unknown or sensitive fields.
7. Agent validates required fields.
8. Agent stops before final submit.
9. User reviews and submits manually.
10. Agent records final status.

## 9. MVP Scope

Included:

- Local project scaffold.
- Configuration for LLM API key.
- Resume template indexing from the existing English resume folder.
- Manual JD import.
- At least one compliant job source connector.
- JD parsing and fit scoring.
- Resume template selection.
- Tailored resume generation from existing DOCX templates.
- PDF export and page-count check.
- Application tracker.
- Browser form-fill prototype that stops before Submit.

Not included in MVP:

- LinkedIn scraping.
- LinkedIn auto-apply.
- Unrestricted browser auto-submit.
- Fully autonomous high-volume application blasting.
- Email inbox parsing.
- Calendar scheduling.
- Interview prep automation.
- Outcome analytics beyond basic status tracking.

## 10. Implementation Approach Options

### Option A: CLI-first local agent

Build a Python CLI that ingests jobs, generates materials, and launches browser fill sessions.

Pros:

- Fastest to build.
- Easy to debug.
- Works well for a lesson/project environment.
- Lower frontend overhead.

Cons:

- Less polished user experience.
- Reviewing many jobs is less convenient than in a dashboard.

### Option B: Local web dashboard

Build a local web app with job queue, fit scores, generated documents, and application status.

Pros:

- Best user experience.
- Easier to review jobs and materials.
- Good long-term foundation.

Cons:

- More implementation work.
- Requires frontend design and state management.

### Option C: Hybrid CLI plus generated reports

Build CLI workflows but export Markdown/HTML review pages for job batches.

Pros:

- Balanced complexity.
- Review experience is better than pure CLI.
- Can evolve into a dashboard later.

Cons:

- Still has two interfaces.

Recommendation: start with Option C. Use a CLI for deterministic operations and generate a simple local HTML/Markdown review report for jobs and tailored materials. This gets the core agent working quickly while keeping the UX usable.

## 11. Safety and Compliance Boundaries

The agent must include explicit gates:

- Source gate: only use configured sources that are allowed by API terms, RSS, user-provided content, or public ATS endpoints.
- Truth gate: generated resume and answers must be grounded in known user facts.
- Sensitive-field gate: sensitive answers require saved confirmation or user review.
- Submit gate: browser-based final submission requires user confirmation.
- Rate gate: job source calls must use reasonable rate limits and respect API limits.
- Audit gate: every generated material and application action must be logged.

## 12. First Build Milestones

Milestone 1: Project foundation

- Initialize app structure.
- Add environment configuration.
- Add database schema.
- Index existing resume templates.

Milestone 2: JD intake and scoring

- Add manual JD import.
- Add one public job-source connector.
- Implement JD parser.
- Implement fit scorer and role classifier.

Milestone 3: Resume tailoring

- Select template from the seven existing resumes.
- Generate edit plan.
- Apply safe DOCX edits.
- Export PDF.
- Run page-count and text checks.

Milestone 4: Application preparation

- Generate cover letter and screening answers.
- Create application record.
- Export review packet.

Milestone 5: Browser form fill

- Open apply URL.
- Inspect fields.
- Fill standard fields.
- Upload resume.
- Stop before Submit.
- Save status.

## 13. Default Implementation Decisions

These defaults make the next implementation plan executable. The user can override any of them before implementation starts.

- First interface: hybrid CLI plus generated Markdown/HTML review reports.
- First job intake: manual JD import plus public-source connectors for Remotive, Greenhouse, and Lever, in that order.
- LLM provider: OpenAI-compatible API configured through environment variables.
- Resume style: preserve the original DOCX template style and page budget whenever possible.
- Resume strategy: use the seven existing role-specific resumes as templates rather than creating a master resume database in MVP.
- Sensitive answers: default to `requires_review` until the user explicitly saves an approved answer.
- Submission policy: browser form filling is allowed, but final Submit remains manual unless a source-specific adapter explicitly supports permitted auto-submit.
- Storage: SQLite for structured state, local folders for generated documents, and Markdown/HTML exports for review.

## 14. Approval Criteria

The design is ready for implementation when:

- The user accepts the human-confirmed submission boundary.
- The user accepts the compliant-source boundary.
- The user accepts the default implementation decisions or provides overrides.
- The user accepts the seven-template resume strategy.
- The implementation plan breaks the design into independently testable milestones.
