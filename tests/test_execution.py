from pathlib import Path
from subprocess import CompletedProcess

from job_agent.execution import execute_application_batch


def test_execute_application_batch_records_success_without_sensitive_stdout(tmp_path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text("console.log('candidate@example.com')")

    def fake_run(command, **kwargs):
        assert command == ["node", str(script)]
        assert kwargs["timeout"] == 300
        return CompletedProcess(command, 0, stdout="candidate@example.com", stderr="")

    records = execute_application_batch(
        [{"company": "Acme", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records == [
        {
            "company": "Acme",
            "title": "Agent Engineer",
            "script_path": str(script),
            "status": "autofill_completed_pending_human_confirmation",
            "exit_code": 0,
            "submit_gate": "blocked_pending_human_confirmation",
            "error": None,
        }
    ]
    assert "candidate@example.com" not in str(records)


def test_execute_application_batch_records_missing_script_and_failure(tmp_path):
    failed_script = tmp_path / "failed.js"
    failed_script.write_text("throw new Error('failed')")

    def fake_run(command, **kwargs):
        return CompletedProcess(command, 2, stdout="", stderr="browser failed")

    records = execute_application_batch(
        [
            {"company": "Missing", "title": "Role", "runtime_script_path": None},
            {"company": "Broken", "title": "Role", "runtime_script_path": str(failed_script)},
        ],
        runner=fake_run,
    )

    assert records[0]["status"] == "skipped_missing_runtime_script"
    assert records[0]["exit_code"] is None
    assert records[1]["status"] == "autofill_failed"
    assert records[1]["exit_code"] == 2
    assert records[1]["error"] == "runtime_script_nonzero_exit"
    assert all(record["submit_gate"] == "blocked_pending_human_confirmation" for record in records)
