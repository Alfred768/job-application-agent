"""Application tracking tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hello_agents.tools.base import Tool, ToolParameter
from job_agent.db import connect, create_application, create_job, init_db
from job_agent.jobs import import_job_from_text


class ApplicationTrackerTool(Tool):
    """Create auditable job and application records in SQLite."""

    def __init__(self):
        super().__init__(
            name="application_tracker",
            description="Create job and application records with needs_review status.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        database_path = Path(parameters.get("database_path") or "job-agent.db")
        jd_text = parameters.get("jd_text") or parameters.get("input") or ""
        job = import_job_from_text(jd_text)
        conn = connect(database_path)
        init_db(conn)
        job_id = create_job(conn, job)
        application_id = create_application(conn, job_id, job)
        return (
            f"job_id={job_id}\n"
            f"application_id={application_id}\n"
            "status=needs_review"
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
