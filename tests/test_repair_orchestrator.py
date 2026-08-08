from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from job_agent.repair_orchestrator import (
    RepairPolicy,
    build_repair_request,
    check_repair_agent_readiness,
    promote_deferred_repair,
    run_repair_cycle,
)


@pytest.fixture(autouse=True)
def _isolated_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return codex_home


def _write_repair_workspace(root: Path) -> None:
    (root / "src" / "job_agent").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "ops").mkdir()
    (root / "profiles").mkdir()
    (root / "examples").mkdir()
    (root / "scripts").mkdir()
    (root / "AGENTS.md").write_text("# Fixture instructions\n")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (root / "ops" / "daily.local.json").write_text('{"private": true}\n')
    (root / "profiles" / "private-profile.json").write_text('{"private": true}\n')
    (root / "src" / "job_agent" / "runtime.py").write_text("VALUE = 'broken'\n")
    (root / "tests" / "test_runtime.py").write_text(
        "def test_fixture():\n    assert True\n"
    )


def test_build_repair_request_fingerprints_country_and_repeated_timeout_fields(
    tmp_path: Path,
) -> None:
    timeout_evidence = tmp_path / "applications" / "waymo" / "execution-timeout.txt"
    timeout_evidence.parent.mkdir(parents=True)
    timeout_evidence.write_text(
        "\n".join(
            [
                "status: autofill_timed_out",
                "Autofill field: Departments (combobox)",
                "Autofill field: Locations (combobox)",
                "Autofill field: Departments (combobox)",
                "Autofill field: Locations (combobox)",
            ]
        )
    )
    audit = {
        "applications": [
            {
                "company": "Example",
                "title": "Engineer",
                "status": "autofill_completed_blocked",
                "review_items": [
                    {
                        "label": "Country",
                        "reason": (
                            "fill error: no combobox option matches saved answer; "
                            "available options: Select..."
                        ),
                        "sensitive": False,
                        "blocking": True,
                    }
                ],
            },
            {
                "company": "Waymo",
                "title": "Engineer",
                "status": "autofill_timed_out",
                "evidence": str(timeout_evidence),
            },
        ]
    }

    request = build_repair_request(audit, run_dir=tmp_path, cycle=1)

    assert request is not None
    codes = {
        fingerprint["code"]
        for finding in request["findings"]
        for fingerprint in finding["fingerprints"]
    }
    assert "country_combobox_commit_mismatch" in codes
    assert "combobox_no_progress_timeout" in codes
    waymo = next(
        finding for finding in request["findings"] if finding["company"] == "Waymo"
    )
    assert waymo["repeated_fields"] == ["Departments", "Locations"]
    serialized = json.dumps(request)
    assert "candidate facts" not in serialized.lower()


def test_build_repair_request_covers_navigation_and_unmapped_field_defects(
    tmp_path: Path,
) -> None:
    audit = {
        "applications": [
            {
                "company": "Stripe",
                "title": "Security Engineer",
                "status": "autofill_failed",
                "review_items": [
                    {
                        "label": "Application form",
                        "reason": "no visible job-application form was found",
                        "sensitive": False,
                        "blocking": True,
                    }
                ],
            },
            {
                "company": "Example",
                "title": "Engineer",
                "status": "autofill_completed_blocked",
                "review_items": [
                    {
                        "label": "New ATS field",
                        "reason": "unmapped field",
                        "sensitive": False,
                        "blocking": True,
                    },
                    {
                        "label": "Work authorization",
                        "reason": "no approved answer",
                        "sensitive": True,
                        "blocking": True,
                    },
                ],
            },
        ]
    }

    request = build_repair_request(audit, run_dir=tmp_path, cycle=1)

    assert request is not None
    codes = {
        fingerprint["code"]
        for finding in request["findings"]
        for fingerprint in finding["fingerprints"]
    }
    assert "application_form_navigation_failure" in codes
    assert "unmapped_field_classification_gap" in codes
    serialized = json.dumps(request)
    assert "Work authorization" not in serialized


def test_build_repair_request_excludes_user_authored_candidate_answers(
    tmp_path: Path,
) -> None:
    label = (
        "What's the most interesting paper, blog post, or documentation "
        "you've read in the past month?"
    )
    audit = {
        "applications": [
            {
                "company": "Netic",
                "title": "Engineer",
                "status": "autofill_completed_blocked",
                "review_items": [
                    {
                        "label": label,
                        "reason": "unmapped field",
                        "sensitive": False,
                        "blocking": True,
                    }
                ],
            },
            {
                "company": "Stripe",
                "title": "Engineer",
                "status": "autofill_failed",
                "review_items": [
                    {
                        "label": "Application form",
                        "reason": "no visible job-application form was found",
                        "sensitive": False,
                        "blocking": True,
                    }
                ],
            },
            {
                "company": "Palantir",
                "title": "Software Engineer, New Grad",
                "status": "autofill_completed_blocked",
                "review_items": [
                    {
                        "label": "High School Name",
                        "reason": "unmapped field",
                        "sensitive": False,
                        "blocking": True,
                    },
                    {
                        "label": "Year of High School Graduation",
                        "reason": "no matching option / answer",
                        "sensitive": False,
                        "blocking": True,
                    },
                ],
            },
        ]
    }

    request = build_repair_request(audit, run_dir=tmp_path, cycle=1)

    assert request is not None
    assert [finding["company"] for finding in request["findings"]] == [
        "Stripe"
    ]
    assert label not in json.dumps(request)
    assert "High School" not in json.dumps(request)


def test_repair_readiness_reports_expired_authentication(
    tmp_path: Path,
) -> None:
    agent = tmp_path / "codex"
    agent.write_text("#!/bin/sh\n")
    agent.chmod(0o755)

    def expired(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="invalid_refresh_token: token_expired",
        )

    readiness = check_repair_agent_readiness(
        RepairPolicy(enabled=True, agent_binary=str(agent)),
        runner=expired,
    )

    assert readiness.ready is False
    assert readiness.code == "repair_agent_authentication_failed"


def test_repair_readiness_rejects_login_status_false_positive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = tmp_path / "codex"
    agent.write_text("#!/bin/sh\n")
    agent.chmod(0o755)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    commands: list[list[str]] = []

    def status_then_expired(command, **kwargs):
        commands.append(list(command))
        if command[1:3] == ["login", "status"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="Logged in using ChatGPT",
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="401 Unauthorized: invalid_refresh_token",
        )

    readiness = check_repair_agent_readiness(
        RepairPolicy(enabled=True, agent_binary=str(agent)),
        runner=status_then_expired,
    )

    assert readiness.ready is False
    assert readiness.code == "repair_agent_authentication_failed"
    assert [command[1] for command in commands] == ["login", "exec"]


def test_repair_readiness_uses_ephemeral_api_key_for_remote_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = tmp_path / "codex"
    agent.write_text("#!/bin/sh\n")
    agent.chmod(0o755)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-api-key")
    commands: list[list[str]] = []

    def ready(command, **kwargs):
        commands.append(list(command))
        assert command[1] == "exec"
        assert kwargs["env"]["CODEX_API_KEY"] == "fixture-api-key"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="READY",
            stderr="",
        )

    readiness = check_repair_agent_readiness(
        RepairPolicy(enabled=True, agent_binary=str(agent)),
        runner=ready,
    )

    assert readiness.ready is True
    assert len(commands) == 1


def test_repair_readiness_projects_selected_custom_provider_without_secret_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_codex_home: Path,
) -> None:
    agent = tmp_path / "codex"
    agent.write_text("#!/bin/sh\n")
    agent.chmod(0o755)
    (_isolated_codex_home / "config.toml").write_text(
        "\n".join(
            [
                'model = "gpt-fixture"',
                'model_provider = "custom"',
                "",
                "[model_providers.custom]",
                'name = "Fixture proxy"',
                'base_url = "https://proxy.example.test/v1"',
                'env_key = "FIXTURE_PROXY_API_KEY"',
                'wire_api = "responses"',
                "requires_openai_auth = false",
            ]
        )
    )
    monkeypatch.setenv("FIXTURE_PROXY_API_KEY", "fixture-secret")
    monkeypatch.setenv("CODEX_API_KEY", "wrong-default-key")
    commands: list[list[str]] = []

    def ready(command, **kwargs):
        commands.append(list(command))
        assert command[1] == "exec"
        assert "--ignore-user-config" in command
        command_text = " ".join(command)
        assert 'model_provider="custom"' in command_text
        assert (
            'model_providers.custom.base_url='
            '"https://proxy.example.test/v1"'
        ) in command_text
        assert (
            'model_providers.custom.env_key="FIXTURE_PROXY_API_KEY"'
        ) in command_text
        assert "fixture-secret" not in command_text
        assert kwargs["env"]["FIXTURE_PROXY_API_KEY"] == "fixture-secret"
        assert "CODEX_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["CODEX_HOME"] != str(_isolated_codex_home)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="READY",
            stderr="",
        )

    readiness = check_repair_agent_readiness(
        RepairPolicy(enabled=True, agent_binary=str(agent)),
        runner=ready,
    )

    assert readiness.ready is True
    assert readiness.code == "ready"
    assert "provider custom" in readiness.message
    assert len(commands) == 1


def test_repair_readiness_retries_isolated_directory_cleanup_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = tmp_path / "codex"
    agent.write_text("#!/bin/sh\n")
    agent.chmod(0o755)
    original_rmtree = __import__("shutil").rmtree
    cleanup_attempts = 0

    def flaky_rmtree(path, *args, **kwargs):
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            raise OSError(66, "Directory not empty", str(path))
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        "job_agent.repair_orchestrator.shutil.rmtree",
        flaky_rmtree,
    )

    readiness = check_repair_agent_readiness(
        RepairPolicy(enabled=True, agent_binary=str(agent)),
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="READY",
            stderr="",
        ),
    )

    assert readiness.ready is True
    assert cleanup_attempts == 2


def test_repair_readiness_does_not_fall_back_when_custom_provider_key_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_codex_home: Path,
) -> None:
    agent = tmp_path / "codex"
    agent.write_text("#!/bin/sh\n")
    agent.chmod(0o755)
    (_isolated_codex_home / "config.toml").write_text(
        "\n".join(
            [
                'model_provider = "custom"',
                "[model_providers.custom]",
                'base_url = "https://proxy.example.test/v1"',
                'env_key = "FIXTURE_PROXY_API_KEY"',
                'wire_api = "responses"',
            ]
        )
    )
    monkeypatch.delenv("FIXTURE_PROXY_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_API_KEY", "must-not-be-used")

    readiness = check_repair_agent_readiness(
        RepairPolicy(enabled=True, agent_binary=str(agent)),
        runner=lambda *_args, **_kwargs: pytest.fail(
            "missing custom-provider credentials must stop before Codex starts"
        ),
    )

    assert readiness.ready is False
    assert readiness.code == "repair_agent_provider_key_missing"
    assert "FIXTURE_PROXY_API_KEY" in readiness.message


def test_repair_cycle_classifies_expired_authentication_as_unavailable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    run_dir = root / "output" / "daily" / "run"
    run_dir.mkdir(parents=True)
    _write_repair_workspace(root)

    result = run_repair_cycle(
        RepairPolicy(enabled=True, agent_binary="codex", max_cycles=2),
        root=root,
        run_dir=run_dir,
        request={"schema_version": 1, "cycle": 1, "findings": []},
        agent_runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Provided authentication token is expired",
        ),
    )

    assert result["status"] == "agent_unavailable"
    assert result["reason"] == "repair_agent_authentication_failed"
    assert result["retryable"] is False


def test_repair_cycle_uses_same_projected_provider_as_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_codex_home: Path,
) -> None:
    root = tmp_path / "workspace"
    run_dir = root / "output" / "daily" / "run"
    run_dir.mkdir(parents=True)
    _write_repair_workspace(root)
    (_isolated_codex_home / "config.toml").write_text(
        "\n".join(
            [
                'model = "gpt-fixture"',
                'model_provider = "custom"',
                "[model_providers.custom]",
                'base_url = "https://proxy.example.test/v1"',
                'env_key = "FIXTURE_PROXY_API_KEY"',
                'wire_api = "responses"',
                "requires_openai_auth = false",
            ]
        )
    )
    monkeypatch.setenv("FIXTURE_PROXY_API_KEY", "fixture-secret")

    def fake_agent(command, **kwargs):
        command_text = " ".join(command)
        assert 'model_provider="custom"' in command_text
        assert "fixture-secret" not in command_text
        assert kwargs["env"]["FIXTURE_PROXY_API_KEY"] == "fixture-secret"
        assert kwargs["env"]["CODEX_HOME"] != str(_isolated_codex_home)
        staging = Path(command[command.index("-C") + 1])
        (staging / "src" / "job_agent" / "runtime.py").write_text(
            "VALUE = 'fixed-with-custom-provider'\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout="fixed", stderr="")

    result = run_repair_cycle(
        RepairPolicy(enabled=True, agent_binary="codex", max_cycles=1),
        root=root,
        run_dir=run_dir,
        request={"schema_version": 1, "cycle": 1, "findings": []},
        agent_runner=fake_agent,
        verification_runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="passed",
            stderr="",
        ),
        verification_commands=(("verify",),),
    )

    assert result["status"] == "promoted"
    assert (
        root / "src" / "job_agent" / "runtime.py"
    ).read_text() == "VALUE = 'fixed-with-custom-provider'\n"
    assert "fixture-secret" not in json.dumps(result)


def test_isolated_repair_promotes_only_after_all_verification_passes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    run_dir = root / "output" / "daily" / "run"
    run_dir.mkdir(parents=True)
    _write_repair_workspace(root)
    verification_calls: list[list[str]] = []

    def fake_agent(command, **kwargs):
        staging = Path(command[command.index("-C") + 1])
        assert "--ignore-user-config" in command
        assert not (staging / "profiles").exists()
        assert not (staging / "ops" / "daily.local.json").exists()
        assert "RESUME_SOURCE_DIR" not in kwargs["env"]
        (staging / "src" / "job_agent" / "runtime.py").write_text(
            "VALUE = 'fixed'\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout="fixed", stderr="")

    def fake_verifier(command, **kwargs):
        verification_calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="passed", stderr="")

    result = run_repair_cycle(
        RepairPolicy(
            enabled=True,
            agent_binary=sys.executable,
            max_cycles=1,
            agent_timeout_seconds=30,
            verification_timeout_seconds=30,
        ),
        root=root,
        run_dir=run_dir,
        request={"schema_version": 1, "cycle": 1, "findings": []},
        agent_runner=fake_agent,
        verification_runner=fake_verifier,
        verification_commands=(("verify-one",), ("verify-two",)),
    )

    assert result["status"] == "promoted"
    assert result["changed_files"] == ["src/job_agent/runtime.py"]
    assert (root / "src" / "job_agent" / "runtime.py").read_text() == (
        "VALUE = 'fixed'\n"
    )
    assert verification_calls == [["verify-one"], ["verify-two"]]
    assert Path(result["result_path"]).is_file()


def test_no_diff_repair_is_verified_before_being_marked_already_fixed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    run_dir = root / "output" / "daily" / "run"
    run_dir.mkdir(parents=True)
    _write_repair_workspace(root)
    verification_calls: list[list[str]] = []

    def unchanged_agent(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="The requested behavior is already covered.",
            stderr="",
        )

    def passing_verifier(command, **kwargs):
        verification_calls.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="passed",
            stderr="",
        )

    result = run_repair_cycle(
        RepairPolicy(
            enabled=True,
            agent_binary=sys.executable,
            max_cycles=1,
            agent_timeout_seconds=30,
            verification_timeout_seconds=30,
        ),
        root=root,
        run_dir=run_dir,
        request={"schema_version": 1, "cycle": 1, "findings": []},
        agent_runner=unchanged_agent,
        verification_runner=passing_verifier,
        verification_commands=(("verify-target",), ("verify-all",)),
    )

    assert result["status"] == "already_fixed_verified"
    assert result["changed_files"] == []
    assert result["reason"] == (
        "repair_agent_made_no_changes_all_verification_passed"
    )
    assert verification_calls == [["verify-target"], ["verify-all"]]
    assert all(item["status"] == "passed" for item in result["verification"])


def test_no_diff_repair_still_fails_when_verification_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    run_dir = root / "output" / "daily" / "run"
    run_dir.mkdir(parents=True)
    _write_repair_workspace(root)

    result = run_repair_cycle(
        RepairPolicy(
            enabled=True,
            agent_binary=sys.executable,
            max_cycles=1,
        ),
        root=root,
        run_dir=run_dir,
        request={"schema_version": 1, "cycle": 1, "findings": []},
        agent_runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="unchanged",
            stderr="",
        ),
        verification_runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout="failed",
            stderr="regression",
        ),
        verification_commands=(("verify",),),
    )

    assert result["status"] == "verification_failed"
    assert result["reason"] == "verification_command_failed"
    assert result["agent_loop"]["rounds"][0]["thought"][
        "selected_tool"
    ] == "codex_repair_agent"
    assert result["agent_loop"]["rounds"][0]["memory_update"][
        "short_term_updated"
    ] is True


def test_incremental_repair_defers_main_workspace_promotion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    run_dir = root / "output" / "daily" / "run"
    run_dir.mkdir(parents=True)
    _write_repair_workspace(root)
    target = root / "src" / "job_agent" / "runtime.py"

    def fake_agent(command, **kwargs):
        staging = Path(command[command.index("-C") + 1])
        (staging / "src" / "job_agent" / "runtime.py").write_text(
            "VALUE = 'fixed-incrementally'\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout="fixed", stderr="")

    result = run_repair_cycle(
        RepairPolicy(enabled=True, agent_binary="codex", max_cycles=1),
        root=root,
        run_dir=run_dir,
        request={"schema_version": 1, "cycle": 1, "findings": []},
        agent_runner=fake_agent,
        verification_runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="passed",
            stderr="",
        ),
        verification_commands=(("verify",),),
        defer_promotion=True,
    )

    assert result["status"] == "verified_pending_promotion"
    assert target.read_text() == "VALUE = 'broken'\n"

    promoted = promote_deferred_repair(
        root=root,
        run_dir=run_dir,
        result=result,
    )

    assert promoted["status"] == "promoted"
    assert target.read_text() == "VALUE = 'fixed-incrementally'\n"


def test_isolated_repair_does_not_promote_when_verification_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    run_dir = root / "output" / "daily" / "run"
    run_dir.mkdir(parents=True)
    _write_repair_workspace(root)
    original = (root / "src" / "job_agent" / "runtime.py").read_text()

    def fake_agent(command, **kwargs):
        staging = Path(command[command.index("-C") + 1])
        (staging / "src" / "job_agent" / "runtime.py").write_text(
            "VALUE = 'unverified'\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout="changed", stderr="")

    result = run_repair_cycle(
        RepairPolicy(
            enabled=True,
            agent_binary=sys.executable,
            max_cycles=1,
            agent_timeout_seconds=30,
            verification_timeout_seconds=30,
        ),
        root=root,
        run_dir=run_dir,
        request={"schema_version": 1, "cycle": 1, "findings": []},
        agent_runner=fake_agent,
        verification_runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout="failed",
            stderr="regression",
        ),
        verification_commands=(("verify",),),
    )

    assert result["status"] == "verification_failed"
    assert (root / "src" / "job_agent" / "runtime.py").read_text() == original


def test_default_full_verification_uses_frozen_pre_repair_tests(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    run_dir = root / "output" / "daily" / "run"
    run_dir.mkdir(parents=True)
    _write_repair_workspace(root)
    verification_commands: list[list[str]] = []

    def fake_agent(command, **kwargs):
        staging = Path(command[command.index("-C") + 1])
        (staging / "src" / "job_agent" / "runtime.py").write_text(
            "VALUE = 'fixed'\n"
        )
        (staging / "tests" / "test_runtime.py").write_text(
            "def test_fixture():\n    assert False is True\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout="changed", stderr="")

    def fake_verifier(command, **kwargs):
        verification_commands.append(list(command))
        if "-c" in command:
            trusted_project = Path(command[command.index("-c") + 1]).parent
            trusted_test = (
                trusted_project / "tests" / "test_runtime.py"
            ).read_text()
            assert "assert True" in trusted_test
            assert "assert False is True" not in trusted_test
        return subprocess.CompletedProcess(command, 0, stdout="passed", stderr="")

    result = run_repair_cycle(
        RepairPolicy(
            enabled=True,
            agent_binary=sys.executable,
            max_cycles=1,
            agent_timeout_seconds=30,
            verification_timeout_seconds=30,
        ),
        root=root,
        run_dir=run_dir,
        request={"schema_version": 1, "cycle": 1, "findings": []},
        agent_runner=fake_agent,
        verification_runner=fake_verifier,
    )

    assert result["status"] == "promoted"
    assert len(verification_commands) == 3
    assert "-c" in verification_commands[1]


def test_isolated_repair_rejects_changes_outside_the_allowlist(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    run_dir = root / "output" / "daily" / "run"
    run_dir.mkdir(parents=True)
    _write_repair_workspace(root)
    original = (root / "pyproject.toml").read_text()

    def fake_agent(command, **kwargs):
        staging = Path(command[command.index("-C") + 1])
        (staging / "pyproject.toml").write_text("[project]\nname='unsafe-change'\n")
        return subprocess.CompletedProcess(command, 0, stdout="changed", stderr="")

    result = run_repair_cycle(
        RepairPolicy(
            enabled=True,
            agent_binary=sys.executable,
            max_cycles=1,
            agent_timeout_seconds=30,
            verification_timeout_seconds=30,
        ),
        root=root,
        run_dir=run_dir,
        request={"schema_version": 1, "cycle": 1, "findings": []},
        agent_runner=fake_agent,
        verification_runner=lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
        ),
        verification_commands=(("verify",),),
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "disallowed_file_changes"
    assert result["disallowed_files"] == ["pyproject.toml"]
    assert (root / "pyproject.toml").read_text() == original
