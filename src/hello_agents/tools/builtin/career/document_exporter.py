"""Application package exporter tool."""

from __future__ import annotations

from typing import Any

from hello_agents.tools.base import Tool, ToolParameter
from job_agent.packages import export_application_package


class ApplicationPackageTool(Tool):
    """Export review artifacts for a job application into a local package directory."""

    def __init__(self):
        super().__init__(
            name="application_package",
            description="Export review, JD analysis, resume edit plan, and submit gate files.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        jd_text = parameters.get("jd_text") or parameters.get("input") or ""
        output_dir = parameters.get("output_dir") or "output/application-package"
        package = export_application_package(jd_text, output_dir)
        return (
            f"package_dir={package.package_dir}\n"
            f"review={package.review_path}\n"
            f"jd_analysis={package.jd_analysis_path}\n"
            f"resume_edit_plan={package.resume_edit_plan_path}\n"
            f"submit_gate={package.submit_gate_path}"
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="jd_text",
                type="string",
                description="Raw JD text used to export the package.",
            ),
            ToolParameter(
                name="output_dir",
                type="string",
                description="Directory to write application package files.",
            ),
        ]
