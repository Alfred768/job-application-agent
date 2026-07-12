from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable


SUBMIT_GATE = "blocked_pending_human_confirmation"


def _record(
    item: dict[str, Any],
    script_path: str | None,
    status: str,
    exit_code: int | None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "company": item.get("company") or "Unknown Company",
        "title": item.get("title") or "Unknown Role",
        "script_path": script_path,
        "status": status,
        "exit_code": exit_code,
        "submit_gate": SUBMIT_GATE,
        "error": error,
    }


def execute_application_batch(
    summary_items: list[dict[str, Any]],
    node_binary: str = "node",
    timeout_seconds: int = 300,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[dict[str, Any]]:
    """Run generated runtime autofill scripts and return privacy-safe records."""
    records = []
    for item in summary_items:
        raw_script_path = item.get("runtime_script_path")
        if not raw_script_path:
            records.append(
                _record(item, None, "skipped_missing_runtime_script", None)
            )
            continue
        script_path = str(raw_script_path)
        if not Path(script_path).is_file():
            records.append(
                _record(item, script_path, "skipped_runtime_script_not_found", None)
            )
            continue
        try:
            result = runner(
                [node_binary, script_path],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            records.append(_record(item, script_path, "autofill_timed_out", None, "timeout"))
            continue
        except OSError as exc:
            records.append(_record(item, script_path, "autofill_failed", None, type(exc).__name__))
            continue

        if result.returncode == 0:
            records.append(
                _record(
                    item,
                    script_path,
                    "autofill_completed_pending_human_confirmation",
                    0,
                )
            )
        else:
            # Browser stderr can contain form values or page text. Keep the
            # audit trail useful without copying arbitrary page content.
            records.append(
                _record(
                    item,
                    script_path,
                    "autofill_failed",
                    result.returncode,
                    "runtime_script_nonzero_exit",
                )
            )
    return records


def summarize_execution(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(records),
        "completed": sum(record["status"].startswith("autofill_completed") for record in records),
        "failed": sum(record["status"] in {"autofill_failed", "autofill_timed_out"} for record in records),
        "skipped": sum(record["status"].startswith("skipped_") for record in records),
    }
