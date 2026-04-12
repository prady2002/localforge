"""Smoke tests for the VerificationRunner — proper pytest format."""

from __future__ import annotations

from pathlib import Path

import pytest

from localforge.core.config import LocalForgeConfig
from localforge.core.models import VerificationResult
from localforge.verifier.runner import VerificationRunner


@pytest.fixture()
def runner(tmp_path: Path) -> VerificationRunner:
    # Create a minimal Python file so detection works
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    cfg = LocalForgeConfig(repo_path=str(tmp_path))
    return VerificationRunner(tmp_path, cfg)


def test_detect_project_type(runner: VerificationRunner) -> None:
    caps = runner.detect_project_type()
    assert caps["has_python"] is True


def test_run_command_success(runner: VerificationRunner) -> None:
    result = runner.run_command('python -c "print(42)"', timeout=10)
    assert result.success
    assert result.exit_code == 0
    assert "42" in result.stdout


def test_run_command_failure(runner: VerificationRunner) -> None:
    result = runner.run_command('python -c "raise ValueError(123)"', timeout=10)
    assert not result.success


def test_summarize_results_mixed(runner: VerificationRunner) -> None:
    ok = VerificationResult(
        success=True, command="echo ok", stdout="ok", stderr="",
        exit_code=0, error_count=0, warning_count=0,
    )
    fail = VerificationResult(
        success=False, command="fail cmd", stdout="", stderr="error",
        exit_code=1, error_count=1, warning_count=0,
    )
    summary = runner.summarize_results([ok, fail])
    assert not summary["all_passed"]
    assert len(summary["failed_commands"]) == 1


def test_summarize_results_all_pass(runner: VerificationRunner) -> None:
    ok = VerificationResult(
        success=True, command="echo ok", stdout="ok", stderr="",
        exit_code=0, error_count=0, warning_count=0,
    )
    assert runner.summarize_results([ok])["all_passed"]


def test_summarize_results_empty(runner: VerificationRunner) -> None:
    assert not runner.summarize_results([])["all_passed"]


def test_parse_python_errors(runner: VerificationRunner) -> None:
    output = (
        "FAILED tests/test_x.py::test_login - AssertionError: expected True\n"
        "src/auth.py:10: error: Incompatible return type\n"
        "src/utils.py:25:3: E501 Line too long\n"
    )
    errors = runner.parse_errors(output)
    assert len(errors) == 3
    assert errors[0]["tool"] == "pytest"
    assert errors[1]["tool"] == "mypy" and errors[1]["line"] == 10
    assert errors[2]["tool"] == "ruff" and errors[2]["line"] == 25
