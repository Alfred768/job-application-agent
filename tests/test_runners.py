import hashlib
import shutil
import subprocess

import pytest

from job_agent.runners import render_batch_fill_runner


def test_render_batch_fill_runner_invokes_runtime_scripts_without_submit_actions():
    script = render_batch_fill_runner(
        [
            {
                "company": "Acme AI",
                "title": "Agent Engineer",
                "runtime_script_path": "/tmp/acme/autofill-runtime.js",
                "fill_script_path": "/tmp/acme/fill-form.js",
            },
            {
                "company": "No Form Co",
                "title": "Research Engineer",
                "fill_script_path": None,
            },
        ]
    )

    assert 'spawn("node", [application.script_path]' in script
    assert 'stdio: ["inherit", "pipe", "pipe"]' in script
    assert "/tmp/acme/autofill-runtime.js" in script
    assert "/tmp/acme/fill-form.js" not in script
    assert "No Form Co" not in script
    assert "Runtime completed for each application." in script
    assert "Submit policy: final Submit is automatic unless JOB_AGENT_SUBMIT_COMPLETE=0." in script
    assert ".click(" not in script
    assert ".press(" not in script
    assert ".submit(" not in script


def test_render_batch_fill_runner_falls_back_to_snapshot_fill_scripts():
    script = render_batch_fill_runner(
        [
            {
                "company": "Acme AI",
                "title": "Agent Engineer",
                "fill_script_path": "/tmp/acme/fill-form.js",
            },
        ]
    )

    assert "/tmp/acme/fill-form.js" in script


def test_render_batch_fill_runner_executes_generated_runner_with_node(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runner execution test")

    first = tmp_path / "first-runtime.js"
    second = tmp_path / "second-runtime.js"
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    first.write_text("console.log('first runtime reached'); console.log('Submit gate: STOPPED before final Submit');")
    second.write_text("console.log('second runtime reached'); console.log('Submit gate: STOPPED before final Submit');")
    runner_path = tmp_path / "run-batch.js"
    runner_path.write_text(
        render_batch_fill_runner(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(first),
                    "upload_resume_path": str(resume),
                },
                {
                    "company": "WebCo",
                    "title": "Backend Engineer",
                    "runtime_script_path": str(second),
                    "upload_resume_path": str(resume),
                },
            ]
        )
    )

    result = subprocess.run(
        ["node", str(runner_path)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Resume preflight verified for 2 application(s)." in result.stdout
    assert "Preparing Acme AI - Agent Engineer" in result.stdout
    assert "first runtime reached" in result.stdout
    assert "second runtime reached" in result.stdout
    assert "Submit policy: final Submit is automatic unless JOB_AGENT_SUBMIT_COMPLETE=0." in result.stdout


def test_render_batch_fill_runner_fails_without_child_submit_gate_marker(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runner execution test")

    script = tmp_path / "runtime-without-gate.js"
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    script.write_text("console.log('filled fields but no gate');")
    runner_path = tmp_path / "run-batch.js"
    runner_path.write_text(
        render_batch_fill_runner(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(script),
                    "upload_resume_path": str(resume),
                },
            ]
        )
    )

    result = subprocess.run(
        ["node", str(runner_path)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "Resume preflight verified for 1 application(s)." in result.stdout
    assert "filled fields but no gate" in result.stdout
    assert "runtime completion was not confirmed" in result.stderr


def test_render_batch_fill_runner_blocks_runtime_when_resume_preflight_fails(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runner execution test")

    child = tmp_path / "runtime.js"
    child.write_text("console.log('runtime should not be reached'); console.log('Submit gate: STOPPED before final Submit');")
    resume = tmp_path / "resume.docx"
    resume.write_bytes(b"docx")
    runner_path = tmp_path / "run-batch.js"
    runner_path.write_text(
        render_batch_fill_runner(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(child),
                    "upload_resume_path": str(resume),
                },
            ]
        )
    )

    result = subprocess.run(
        ["node", str(runner_path)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "runtime should not be reached" not in result.stdout
    assert "Resume preflight failed; no browser runtime executed." in result.stderr
    assert "resume upload must be a PDF" in result.stderr


def test_render_batch_fill_runner_blocks_runtime_when_runtime_resume_mismatches_summary(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runner execution test")

    summary_resume = tmp_path / "summary.pdf"
    summary_resume.write_bytes(b"%PDF-1.4\nsummary")
    runtime_resume = tmp_path / "runtime.pdf"
    runtime_resume.write_bytes(b"%PDF-1.4\nruntime")
    child = tmp_path / "runtime.js"
    child.write_text(
        f"const CFG = {{\"resumeFile\": \"{runtime_resume}\"}};\n"
        "console.log('runtime should not be reached');\n"
        "console.log('Submit gate: STOPPED before final Submit');\n"
    )
    runner_path = tmp_path / "run-batch.js"
    runner_path.write_text(
        render_batch_fill_runner(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(child),
                    "upload_resume_path": str(summary_resume),
                },
            ]
        )
    )

    result = subprocess.run(
        ["node", str(runner_path)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "runtime should not be reached" not in result.stdout
    assert "Resume preflight failed; no browser runtime executed." in result.stderr
    assert "runtime resumeFile does not match summary upload_resume_path" in result.stderr


def test_render_batch_fill_runner_blocks_runtime_when_prepared_resume_hash_changes(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runner execution test")

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\noriginal")
    prepared_sha = hashlib.sha256(b"%PDF-1.4\noriginal").hexdigest()
    resume.write_bytes(b"%PDF-1.4\ngenerated replacement")
    child = tmp_path / "runtime.js"
    child.write_text(
        f"const CFG = {{\"resumeFile\": \"{resume}\"}};\n"
        "console.log('runtime should not be reached');\n"
        "console.log('Submit gate: STOPPED before final Submit');\n"
    )
    runner_path = tmp_path / "run-batch.js"
    runner_path.write_text(
        render_batch_fill_runner(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(child),
                    "upload_resume_path": str(resume),
                    "upload_resume_pdf_sha256": prepared_sha,
                },
            ]
        )
    )

    result = subprocess.run(
        ["node", str(runner_path)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "runtime should not be reached" not in result.stdout
    assert "Resume preflight failed; no browser runtime executed." in result.stderr
    assert "hash does not match prepared summary" in result.stderr


def test_render_batch_fill_runner_blocks_runtime_without_resume_path(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runner execution test")

    child = tmp_path / "runtime.js"
    child.write_text(
        "console.log('runtime should not be reached');\n"
        "console.log('Submit gate: STOPPED before final Submit');\n"
    )
    runner_path = tmp_path / "run-batch.js"
    runner_path.write_text(
        render_batch_fill_runner(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(child),
                },
            ]
        )
    )

    result = subprocess.run(
        ["node", str(runner_path)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "runtime should not be reached" not in result.stdout
    assert "Resume preflight failed; no browser runtime executed." in result.stderr
    assert "missing required PDF resume upload path" in result.stderr


def test_render_batch_fill_runner_blocks_resume_outside_required_source_dir(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runner execution test")

    source_dir = tmp_path / "resumes"
    source_dir.mkdir()
    outside_resume = tmp_path / "outside.pdf"
    outside_resume.write_bytes(b"%PDF-1.4\noutside")
    child = tmp_path / "runtime.js"
    child.write_text(
        "console.log('runtime should not be reached');\n"
        "console.log('Submit gate: STOPPED before final Submit');\n"
    )
    runner_path = tmp_path / "run-batch.js"
    runner_path.write_text(
        render_batch_fill_runner(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(child),
                    "upload_resume_path": str(outside_resume),
                },
            ],
            required_resume_source_dir={"path": str(source_dir)},
        )
    )

    result = subprocess.run(
        ["node", str(runner_path)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "runtime should not be reached" not in result.stdout
    assert "Resume preflight failed; no browser runtime executed." in result.stderr
    assert "must come from required resume source dir" in result.stderr


def test_render_batch_fill_runner_uses_summary_required_source_dir(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runner execution test")

    source_dir = tmp_path / "resumes"
    source_dir.mkdir()
    outside_resume = tmp_path / "outside.pdf"
    outside_resume.write_bytes(b"%PDF-1.4\noutside")
    child = tmp_path / "runtime.js"
    child.write_text(
        "console.log('runtime should not be reached');\n"
        "console.log('Submit gate: STOPPED before final Submit');\n"
    )
    runner_path = tmp_path / "run-batch.js"
    runner_path.write_text(
        render_batch_fill_runner(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(child),
                    "upload_resume_path": str(outside_resume),
                    "required_resume_source_dir": str(source_dir),
                },
            ],
        )
    )

    result = subprocess.run(
        ["node", str(runner_path)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "runtime should not be reached" not in result.stdout
    assert "Resume preflight failed; no browser runtime executed." in result.stderr
    assert "must come from required resume source dir" in result.stderr


def test_render_batch_fill_runner_passes_stdin_to_headed_child_script(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runner execution test")

    child = tmp_path / "headed-runtime.js"
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    child.write_text(
        """
console.log('child waiting for manual review');
process.stdin.resume();
process.stdin.once('data', () => {
  console.log('child received manual review confirmation');
  console.log('Submit gate: STOPPED before final Submit');
});
"""
    )
    runner_path = tmp_path / "run-batch.js"
    runner_path.write_text(
        render_batch_fill_runner(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(child),
                    "upload_resume_path": str(resume),
                },
            ]
        )
    )

    result = subprocess.run(
        ["node", str(runner_path)],
        input="\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "child waiting for manual review" in result.stdout
    assert "child received manual review confirmation" in result.stdout
    assert "Submit policy: final Submit is automatic unless JOB_AGENT_SUBMIT_COMPLETE=0." in result.stdout
