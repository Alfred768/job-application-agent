from typer.testing import CliRunner

from job_agent.cli import app


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
