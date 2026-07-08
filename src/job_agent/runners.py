from __future__ import annotations

import json
from typing import Any


def render_batch_fill_runner(summary_items: list[dict[str, Any]]) -> str:
    runnable_items = [
        {
            "company": item.get("company") or "Unknown Company",
            "title": item.get("title") or "Unknown Role",
            "fill_script_path": item.get("fill_script_path"),
        }
        for item in summary_items
        if item.get("fill_script_path")
    ]
    return "\n".join(
        [
            'const { spawnSync } = require("child_process");',
            "",
            f"const applications = {json.dumps(runnable_items, indent=2)};",
            "",
            "for (const application of applications) {",
            '  console.log(`Preparing ${application.company} - ${application.title}`);',
            '  const result = spawnSync("node", [application.fill_script_path], { stdio: "inherit" });',
            "  if (result.status !== 0) {",
            '    console.error(`Stopped after ${application.company} - ${application.title}`);',
            "    process.exit(result.status || 1);",
            "  }",
            "}",
            "",
            "console.log('Review each page manually before final submission.');",
            "console.log('Submit gate: final Submit remains manual unless an approved source-specific adapter permits it.');",
        ]
    ) + "\n"
