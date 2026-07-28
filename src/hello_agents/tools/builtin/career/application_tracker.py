"""Application tracking tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hello_agents.core.contracts import ToolEffect
from hello_agents.tools.base import Tool, ToolParameter
from job_agent.db import connect, create_application, create_job, init_db
from job_agent.jobs import canonical_job_url, import_job_from_text


def _normalized_application_url(value: str | None) -> str:
    return canonical_job_url(value) or ""


class ApplicationTrackerTool(Tool):
    """Create auditable job and application records in SQLite."""

    def __init__(self):
        super().__init__(
            name="application_tracker",
            description="Create job and application records with needs_review status.",
            effect=ToolEffect.WRITE,
        )

    def run(self, parameters: dict[str, Any]) -> str:
        database_path = Path(parameters.get("database_path") or "job-agent.db")
        jd_text = parameters.get("jd_text") or parameters.get("input") or ""
        job = import_job_from_text(jd_text)
        conn = connect(database_path)
        init_db(conn)
        candidate_url = _normalized_application_url(job.apply_url or job.source_url)
        rows = conn.execute(
            """
            select a.id as application_id, a.job_id, a.status, a.apply_url
            from applications a
            where a.company = ? and a.title = ?
            order by a.id desc
            """,
            (job.company, job.title),
        ).fetchall()
        existing = None
        for row in rows:
            existing_url = _normalized_application_url(row["apply_url"])
            if candidate_url and existing_url == candidate_url:
                existing = row
                break
            if not candidate_url and not existing_url:
                existing = row
                break
        if existing is not None:
            result = (
                f"job_id={existing['job_id']}\n"
                f"application_id={existing['application_id']}\n"
                f"status={existing['status']}"
            )
            conn.close()
            return result
        job_id = create_job(conn, job)
        application_id = create_application(conn, job_id, job)
        tracked = conn.execute(
            "select job_id, status from applications where id = ?",
            (application_id,),
        ).fetchone()
        tracked_job_id = int(tracked["job_id"]) if tracked is not None else job_id
        tracked_status = str(tracked["status"]) if tracked is not None else "needs_review"
        conn.close()
        return (
            f"job_id={tracked_job_id}\n"
            f"application_id={application_id}\n"
            f"status={tracked_status}"
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="database_path",
                type="string",
                description="SQLite database path.",
            ),
            ToolParameter(
                name="jd_text",
                type="string",
                description="Raw JD text used to create the application record.",
            ),
        ]
