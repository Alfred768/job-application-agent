from job_agent.jobs import import_job_from_text
from job_agent.reports import render_markdown_review
from job_agent.scoring import score_fit


def test_render_markdown_review_includes_submit_boundary():
    job = import_job_from_text("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")

    markdown = render_markdown_review(job, score_fit(job))

    assert "# Application Review" in markdown
    assert "Final Submit remains manual" in markdown
    assert "Acme" in markdown
