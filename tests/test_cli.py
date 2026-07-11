from zipfile import ZIP_DEFLATED, ZipFile

from typer.testing import CliRunner

from job_agent.cli import app


def write_minimal_docx(path, paragraphs):
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
        + "</w:body></w:document>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", "<Types></Types>")
        docx.writestr("word/document.xml", document_xml)


def test_cli_init_db(tmp_path):
    runner = CliRunner()

    result = runner.invoke(app, ["init", "--db", str(tmp_path / "agent.db")])

    assert result.exit_code == 0
    assert "Initialized" in result.output


def test_cli_review_job_from_text_file(tmp_path):
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")
    out_path = tmp_path / "review.md"
    runner = CliRunner()

    result = runner.invoke(app, ["jobs", "review", str(jd_path), "--out", str(out_path)])

    assert result.exit_code == 0
    assert out_path.exists()
    text = out_path.read_text()
    assert "Application Review" in text
    assert "## JD Analysis" in text
    assert "## Resume Edit Plan" in text
    assert "## Truthfulness Gate" in text


def test_cli_review_job_can_select_resume_and_track_application(tmp_path):
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    (resume_dir / "GAOYI_WU_Agent_Engineer.docx").write_text("docx")
    db_path = tmp_path / "agent.db"
    out_path = tmp_path / "review.md"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "jobs",
            "review",
            str(jd_path),
            "--out",
            str(out_path),
            "--resume-source-dir",
            str(resume_dir),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0
    text = out_path.read_text()
    assert "## Recommended Resume" in text
    assert "## Resume Edit Plan" in text
    assert "## Tracking" in text
    assert "application_id=1" in text


def test_cli_review_job_can_export_application_package(tmp_path):
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")
    out_path = tmp_path / "review.md"
    package_dir = tmp_path / "package"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "jobs",
            "review",
            str(jd_path),
            "--out",
            str(out_path),
            "--package-dir",
            str(package_dir),
        ],
    )

    assert result.exit_code == 0
    text = out_path.read_text()
    assert "## Application Package" in text
    assert (package_dir / "review.md").exists()
    assert (package_dir / "jd-analysis.json").exists()


def test_cli_review_job_can_include_form_fill_plan(tmp_path):
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Email"}, {"label": "Do you require visa sponsorship?"}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"email": "gaoyi@example.com", "sponsorship": "Needs review"}')
    out_path = tmp_path / "review.md"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "jobs",
            "review",
            str(jd_path),
            "--out",
            str(out_path),
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
        ],
    )

    assert result.exit_code == 0
    text = out_path.read_text()
    assert "## Form Fill Plan" in text
    assert "review_required=Do you require visa sponsorship?" in text


def test_cli_import_rss_jobs_writes_normalized_json(tmp_path):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build LLM agents with FastAPI.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    out_path = tmp_path / "jobs.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "import-rss", str(rss_path), "--out", str(out_path), "--source", "example-rss"],
    )

    assert result.exit_code == 0
    assert "Imported 1 jobs" in result.output
    text = out_path.read_text()
    assert '"title": "Agent Engineer"' in text
    assert '"company": "Acme AI"' in text
    assert '"location": "Remote"' in text


def test_cli_review_rss_jobs_writes_review_packets(tmp_path):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build LLM agents with LangChain and FastAPI.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    out_dir = tmp_path / "reviews"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "review-rss", str(rss_path), "--out-dir", str(out_dir), "--source", "example-rss"],
    )

    assert result.exit_code == 0
    assert "Reviewed 1 jobs" in result.output
    review_files = list(out_dir.glob("*.md"))
    assert len(review_files) == 1
    text = review_files[0].read_text()
    assert "# Application Review" in text
    assert "Agent Engineer" in text
    assert "Acme AI" in text
    assert "## Submit Gate" in text


def test_cli_import_greenhouse_jobs_writes_normalized_json(tmp_path):
    payload_path = tmp_path / "greenhouse.json"
    payload_path.write_text(
        '{"jobs": [{"title": "Agent Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "location": {"name": "Remote"}, "content": "Build agents."}]}'
    )
    out_path = tmp_path / "jobs.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "import-greenhouse", "acme", "--payload", str(payload_path), "--out", str(out_path)],
    )

    assert result.exit_code == 0
    assert "Imported 1 jobs" in result.output
    assert '"source": "greenhouse:acme"' in out_path.read_text()


def test_cli_review_greenhouse_jobs_writes_review_packets(tmp_path):
    payload_path = tmp_path / "greenhouse.json"
    payload_path.write_text(
        '{"jobs": [{"title": "Agent Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "location": {"name": "Remote"}, "content": "Build LLM agents with LangChain."}]}'
    )
    out_dir = tmp_path / "reviews"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "review-greenhouse", "acme", "--payload", str(payload_path), "--out-dir", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "Reviewed 1 jobs" in result.output
    review_files = list(out_dir.glob("*.md"))
    assert len(review_files) == 1
    text = review_files[0].read_text()
    assert "# Application Review" in text
    assert "Agent Engineer" in text
    assert "acme" in text
    assert "## Submit Gate" in text


def test_cli_import_lever_jobs_writes_normalized_json(tmp_path):
    payload_path = tmp_path / "lever.json"
    payload_path.write_text(
        '[{"text": "ML Platform Engineer", "hostedUrl": "https://jobs.lever.co/acme/1", "categories": {"location": "Remote"}, "descriptionPlain": "Build platforms."}]'
    )
    out_path = tmp_path / "jobs.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "import-lever", "acme", "--payload", str(payload_path), "--out", str(out_path)],
    )

    assert result.exit_code == 0
    assert "Imported 1 jobs" in result.output
    assert '"source": "lever:acme"' in out_path.read_text()


def test_cli_review_lever_jobs_writes_review_packets(tmp_path):
    payload_path = tmp_path / "lever.json"
    payload_path.write_text(
        '[{"text": "ML Platform Engineer", "hostedUrl": "https://jobs.lever.co/acme/1", "categories": {"location": "Remote"}, "descriptionPlain": "Build ML platforms with Python."}]'
    )
    out_dir = tmp_path / "reviews"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "review-lever", "acme", "--payload", str(payload_path), "--out-dir", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "Reviewed 1 jobs" in result.output
    review_files = list(out_dir.glob("*.md"))
    assert len(review_files) == 1
    text = review_files[0].read_text()
    assert "# Application Review" in text
    assert "ML Platform Engineer" in text
    assert "acme" in text
    assert "## Submit Gate" in text


def test_cli_import_remotive_jobs_writes_normalized_json(tmp_path):
    payload_path = tmp_path / "remotive.json"
    payload_path.write_text(
        '{"jobs": [{"title": "Backend Engineer", "company_name": "RemoteCo", "url": "https://remotive.com/jobs/1", "candidate_required_location": "Worldwide", "description": "Build APIs."}]}'
    )
    out_path = tmp_path / "jobs.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "import-remotive", "--payload", str(payload_path), "--out", str(out_path)],
    )

    assert result.exit_code == 0
    assert "Imported 1 jobs" in result.output
    assert '"source": "remotive"' in out_path.read_text()


def test_cli_import_sources_combines_configured_sources(tmp_path):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build LLM agents with FastAPI.</description>
        </item></channel></rss>"""
    )
    lever_path = tmp_path / "lever.json"
    lever_path.write_text(
        '[{"text": "ML Platform Engineer", "hostedUrl": "https://jobs.lever.co/acme/1", "categories": {"location": "Remote"}, "descriptionPlain": "Build ML platforms."}]'
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        """
        {
          "sources": [
            {"type": "rss", "source": "example-rss", "rss_file": "jobs.xml"},
            {"type": "lever", "site": "acme", "payload_file": "lever.json"}
          ]
        }
        """
    )
    out_path = tmp_path / "jobs.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "import-sources", str(config_path), "--out", str(out_path)],
    )

    assert result.exit_code == 0
    assert "Imported 2 jobs" in result.output
    text = out_path.read_text()
    assert '"source": "example-rss"' in text
    assert '"source": "lever:acme"' in text


def test_cli_jobs_shortlist_filters_and_scores_jobs(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme",
            "location": "Remote",
            "raw_jd": "Build LangChain agents, RAG workflows, tools, and LLM systems.",
            "source": "test",
            "source_url": "https://jobs.example.com/agent",
            "apply_url": "https://jobs.example.com/agent",
            "remote_policy": null
          },
          {
            "title": "Store Manager",
            "company": "RetailCo",
            "location": "NYC",
            "raw_jd": "Manage retail operations and staffing.",
            "source": "test",
            "source_url": "https://jobs.example.com/store",
            "apply_url": "https://jobs.example.com/store",
            "remote_policy": null
          }
        ]"""
    )
    out_path = tmp_path / "shortlist.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "shortlist", str(jobs_path), "--min-score", "60", "--out", str(out_path)],
    )

    assert result.exit_code == 0
    assert "Shortlisted 1 jobs" in result.output
    text = out_path.read_text()
    assert '"title": "Agent Engineer"' in text
    assert '"fit_score":' in text
    assert "Store Manager" not in text


def test_cli_review_sources_writes_review_packets(tmp_path):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build LLM agents with FastAPI.</description>
        </item></channel></rss>"""
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        """
        {
          "sources": [
            {"type": "rss", "source": "example-rss", "rss_file": "jobs.xml"}
          ]
        }
        """
    )
    out_dir = tmp_path / "reviews"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "review-sources", str(config_path), "--out-dir", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "Reviewed 1 jobs" in result.output
    review_files = list(out_dir.glob("*.md"))
    assert len(review_files) == 1
    assert "# Application Review" in review_files[0].read_text()


def test_cli_review_remotive_jobs_writes_review_packets(tmp_path):
    payload_path = tmp_path / "remotive.json"
    payload_path.write_text(
        '{"jobs": [{"title": "Backend Engineer", "company_name": "RemoteCo", "url": "https://remotive.com/jobs/1", "candidate_required_location": "Worldwide", "description": "Build APIs with FastAPI."}]}'
    )
    out_dir = tmp_path / "reviews"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "review-remotive", "--payload", str(payload_path), "--out-dir", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "Reviewed 1 jobs" in result.output
    review_files = list(out_dir.glob("*.md"))
    assert len(review_files) == 1
    text = review_files[0].read_text()
    assert "# Application Review" in text
    assert "Backend Engineer" in text
    assert "RemoteCo" in text
    assert "## Submit Gate" in text


def test_cli_forms_build_script_writes_guarded_playwright_script(tmp_path):
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Email"}, {"label": "Do you require visa sponsorship?"}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"email": "gaoyi@example.com", "sponsorship": "Needs review"}')
    out_path = tmp_path / "fill-form.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "build-script",
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
            "--application-url",
            "https://jobs.example.com/apply",
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0
    assert "Wrote guarded form-fill script" in result.output
    text = out_path.read_text()
    assert 'await page.goto("https://jobs.example.com/apply");' in text
    assert 'await page.getByLabel("Email").fill("gaoyi@example.com");' in text
    assert "Do you require visa sponsorship?" in text
    assert ".click(" not in text


def test_cli_forms_build_script_can_upload_resume_file(tmp_path):
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Resume", "type": "file", "required": true}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    resume_path = tmp_path / "tailored-resume.pdf"
    resume_path.write_text("pdf")
    out_path = tmp_path / "fill-form.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "build-script",
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
            "--resume-file",
            str(resume_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0
    assert f'await page.getByLabel("Resume").setInputFiles("{resume_path}");' in out_path.read_text()


def test_cli_forms_build_snapshot_script_writes_inspection_only_script(tmp_path):
    out_path = tmp_path / "capture-form.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "build-snapshot-script",
            "--application-url",
            "https://jobs.example.com/apply",
            "--out",
            str(out_path),
            "--snapshot-out",
            "form-snapshot.json",
        ],
    )

    assert result.exit_code == 0
    assert "Wrote guarded form snapshot script" in result.output
    text = out_path.read_text()
    assert 'await page.goto("https://jobs.example.com/apply");' in text
    assert 'fs.writeFileSync("form-snapshot.json"' in text
    assert "querySelectorAll" in text
    assert ".fill(" not in text
    assert ".click(" not in text


def test_cli_applications_prepare_generates_package_and_fill_script(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Email"}, {"label": "Do you require visa sponsorship?"}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"email": "gaoyi@example.com", "sponsorship": "Needs review"}')
    out_dir = tmp_path / "application"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--index",
            "1",
            "--out-dir",
            str(out_dir),
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
        ],
    )

    assert result.exit_code == 0
    assert "Prepared application package" in result.output
    assert (out_dir / "review.md").exists()
    assert (out_dir / "jd-analysis.json").exists()
    assert (out_dir / "resume-edit-plan.json").exists()
    script = (out_dir / "fill-form.js").read_text()
    assert 'await page.goto("https://boards.greenhouse.io/acme/jobs/1");' in script
    assert 'await page.getByLabel("Email").fill("gaoyi@example.com");' in script
    assert ".click(" not in script


def test_cli_applications_prepare_can_generate_tailored_resume_draft(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain, FastAPI, and Rust.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Gaoyi Wu\n\nBuilt Python and FastAPI services.")
    out_dir = tmp_path / "application"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--index",
            "1",
            "--out-dir",
            str(out_dir),
            "--resume",
            str(resume_path),
        ],
    )

    assert result.exit_code == 0
    tailored = (out_dir / "tailored-resume.md").read_text()
    assert "# Tailored Resume Draft" in tailored
    assert "LangChain" in tailored
    assert "Unsupported JD keywords not inserted: Rust" in tailored


def test_cli_applications_prepare_uses_selected_resume_template_text(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain, FastAPI, and RAG.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    write_minimal_docx(
        resume_dir / "GAOYI_WU_Agent_Engineer.docx",
        ["Gaoyi Wu", "Built FastAPI services and LLM workflow tools."],
    )
    out_dir = tmp_path / "application"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--resume-source-dir",
            str(resume_dir),
        ],
    )

    assert result.exit_code == 0
    tailored = (out_dir / "tailored-resume.md").read_text()
    assert "Gaoyi Wu" in tailored
    assert "Built FastAPI services and LLM workflow tools." in tailored
    assert "LangChain" in tailored


def test_cli_applications_prepare_can_wire_tailored_resume_upload(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Resume", "type": "file", "required": true}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Gaoyi Wu\n\nBuilt FastAPI services.")
    out_dir = tmp_path / "application"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
            "--resume",
            str(resume_path),
            "--upload-resume",
        ],
    )

    assert result.exit_code == 0
    script = (out_dir / "fill-form.js").read_text()
    assert 'await page.getByLabel("Resume").setInputFiles(' in script
    assert (out_dir / "tailored-resume.docx").exists()
    assert "tailored-resume.docx" in script
    assert "tailored-resume.md" not in script
    assert ".click(" not in script


def test_cli_applications_prepare_shortlist_generates_batch_packages(tmp_path):
    jobs_path = tmp_path / "shortlist.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null,
            "fit_score": 88
          },
          {
            "title": "Backend Engineer",
            "company": "WebCo",
            "location": "Remote",
            "raw_jd": "Build backend APIs with Postgres and Redis.",
            "source": "lever:webco",
            "source_url": "https://jobs.lever.co/webco/1",
            "apply_url": "https://jobs.lever.co/webco/1",
            "remote_policy": null,
            "fit_score": 76
          }
        ]"""
    )
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Email"}, {"label": "Resume", "type": "file"}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"email": "gaoyi@example.com"}')
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Gaoyi Wu\n\nBuilt FastAPI and Postgres services.")
    out_dir = tmp_path / "batch"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "prepare-shortlist",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--limit",
            "2",
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
            "--resume",
            str(resume_path),
            "--upload-resume",
        ],
    )

    assert result.exit_code == 0
    assert "Prepared 2 application packages" in result.output
    first = out_dir / "001-acme-ai-agent-engineer"
    second = out_dir / "002-webco-backend-engineer"
    assert (first / "review.md").exists()
    assert (first / "tailored-resume.md").exists()
    assert 'await page.goto("https://boards.greenhouse.io/acme/jobs/1");' in (first / "fill-form.js").read_text()
    assert (second / "review.md").exists()
    summary = (out_dir / "batch-summary.json").read_text()
    assert '"package_dir":' in summary
    assert "001-acme-ai-agent-engineer" in summary
    assert "002-webco-backend-engineer" in summary


def test_cli_applications_build_batch_runner_writes_guarded_runner(tmp_path):
    first = tmp_path / "batch" / "001-acme-ai-agent-engineer"
    second = tmp_path / "batch" / "002-webco-backend-engineer"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    summary_path = tmp_path / "batch" / "batch-summary.json"
    summary_path.write_text(
        f"""[
          {{"company": "Acme AI", "title": "Agent Engineer", "fill_script_path": "{first / "fill-form.js"}"}},
          {{"company": "WebCo", "title": "Backend Engineer", "fill_script_path": "{second / "fill-form.js"}"}}
        ]"""
    )
    out_path = tmp_path / "batch" / "run-batch.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "build-batch-runner",
            str(summary_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0
    assert "Wrote guarded batch runner" in result.output
    script = out_path.read_text()
    assert 'spawnSync("node"' in script
    assert str(first / "fill-form.js") in script
    assert str(second / "fill-form.js") in script
    assert "Review each page manually before final submission." in script
    assert ".click(" not in script
    assert ".press(" not in script
    assert ".submit(" not in script


def test_cli_forms_autofill_writes_simplify_style_runtime_script(tmp_path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        '{"name": "Gaoyi Wu", "email": "gaoyi@example.com", '
        '"answers": {"Are you authorized to work in the United States?": "Yes"}}'
    )
    resume_path = tmp_path / "tailored-resume.docx"
    resume_path.write_bytes(b"docx")
    out_path = tmp_path / "autofill.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "autofill",
            "--profile",
            str(profile_path),
            "--out",
            str(out_path),
            "--application-url",
            "https://boards.greenhouse.io/acme/jobs/1",
            "--resume-file",
            str(resume_path),
        ],
    )

    assert result.exit_code == 0
    assert "Simplify-style runtime autofill script" in result.output
    text = out_path.read_text()
    assert 'require("playwright")' in text
    assert "https://boards.greenhouse.io/acme/jobs/1" in text
    assert "Gaoyi Wu" in text
    assert str(resume_path) in text
    # never auto-submits
    assert "STOPPED before final Submit" in text


def test_cli_forms_init_sensitive_kb_writes_template(tmp_path):
    out_path = tmp_path / "sensitive-answers.json"
    runner = CliRunner()

    result = runner.invoke(app, ["forms", "init-sensitive-kb", "--out", str(out_path)])

    assert result.exit_code == 0
    assert "knowledge base template" in result.output
    import json as _json

    kb = _json.loads(out_path.read_text())
    assert "salary" in kb
    assert "work_authorization" in kb
    assert kb["salary"]["approved"] is False


def test_cli_forms_autofill_merges_sensitive_kb(tmp_path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"name": "Gaoyi Wu", "email": "gaoyi@example.com"}')
    kb_path = tmp_path / "sensitive-answers.json"
    kb_path.write_text(
        '{"salary": {"patterns": ["salary"], "answer": "120000", "approved": true}}'
    )
    out_path = tmp_path / "autofill.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "autofill",
            "--profile",
            str(profile_path),
            "--sensitive-kb",
            str(kb_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0
    text = out_path.read_text()
    # the approved KB answer is embedded so the runtime engine can use it
    assert "120000" in text
    assert "sensitive_answers" in text


def test_cli_forms_init_profile_writes_rich_template(tmp_path):
    out_path = tmp_path / "profile.json"
    runner = CliRunner()

    result = runner.invoke(app, ["forms", "init-profile", "--out", str(out_path)])

    assert result.exit_code == 0
    assert "rich profile template" in result.output
    import json as _json

    profile = _json.loads(out_path.read_text())
    assert "work_history" in profile
    assert "education" in profile
    assert "demographics" in profile
    assert "answers" in profile


def test_cli_forms_build_profile_from_resume(tmp_path):
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text(
        "Gaoyi Wu\nNew York, NY  |  gaoyi@example.com\n\n"
        "Experience\nAI Engineer — Acme\nBuilt agents.\n\n"
        "Education\nB.S. CS — State U\n"
    )
    out_path = tmp_path / "profile.json"
    runner = CliRunner()

    result = runner.invoke(
        app, ["forms", "build-profile-from-resume", "--resume", str(resume_path), "--out", str(out_path)]
    )

    assert result.exit_code == 0
    assert "work_history entries: 1" in result.output
    import json as _json

    profile = _json.loads(out_path.read_text())
    assert profile["name"] == "Gaoyi Wu"
    assert profile["work_history"][0]["title"] == "AI Engineer"


def test_cli_resumes_tailor_writes_grounded_resume_draft(tmp_path):
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text("Title: Agent Engineer\n\nBuild LangChain agents with FastAPI and Rust.")
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Gaoyi Wu\n\nBuilt Python and FastAPI services.")
    out_path = tmp_path / "tailored-resume.md"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "resumes",
            "tailor",
            str(jd_path),
            "--resume",
            str(resume_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0
    assert "Wrote tailored resume draft" in result.output
    text = out_path.read_text()
    assert "# Tailored Resume Draft" in text
    assert "LangChain" in text
    assert "Unsupported JD keywords not inserted: Rust" in text


def test_cli_read_json_source_sends_browser_user_agent(monkeypatch):
    """Regression guard: live job APIs (Remotive) 403 the default Python-urllib UA.

    The CLI's autonomous source fetcher must attach a browser-like User-Agent
    so `jobs import-remotive` / `import-greenhouse` / `import-lever` keep working
    against live public endpoints.
    """
    import job_agent.cli as cli

    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"jobs": []}'

    def fake_urlopen(request, timeout=20):
        captured["user_agent"] = request.get_header("User-agent")
        return _FakeResponse()

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)
    cli._read_json_source(None, "https://remotive.com/api/remote-jobs")

    assert captured["user_agent"]
    assert "Python-urllib" not in captured["user_agent"]
    assert "Mozilla" in captured["user_agent"]
