"""Verification runner – detects project tooling and runs checks."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from localforge.core.config import LocalForgeConfig
from localforge.core.models import VerificationResult

console = Console()


class VerificationRunner:
    """Detect installed tooling and run verification commands against the repo."""

    def __init__(self, repo_path: Path, config: LocalForgeConfig) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.config = config

    # ------------------------------------------------------------------
    # Project detection
    # ------------------------------------------------------------------

    def detect_project_type(self) -> dict[str, bool]:
        """Return a dict of detected project capabilities."""
        caps: dict[str, bool] = {}

        # pytest
        has_pytest = False
        if (self.repo_path / "pytest.ini").is_file():
            has_pytest = True
        if (self.repo_path / "setup.cfg").is_file():
            try:
                text = (self.repo_path / "setup.cfg").read_text(encoding="utf-8")
                if "[tool:pytest]" in text:
                    has_pytest = True
            except OSError:
                pass
        if (self.repo_path / "pyproject.toml").is_file():
            try:
                text = (self.repo_path / "pyproject.toml").read_text(encoding="utf-8")
                if "[tool.pytest" in text:
                    has_pytest = True
            except OSError:
                pass
        caps["has_pytest"] = has_pytest

        # npm / package.json
        caps["has_npm"] = (self.repo_path / "package.json").is_file()

        # Makefile
        caps["has_makefile"] = (self.repo_path / "Makefile").is_file()

        # Go
        caps["has_go"] = (self.repo_path / "go.mod").is_file()

        # Python files present
        caps["has_python"] = any(self.repo_path.rglob("*.py"))

        # TypeScript present
        caps["has_typescript"] = any(self.repo_path.rglob("*.ts"))

        return caps

    # ------------------------------------------------------------------
    # Command list
    # ------------------------------------------------------------------

    @staticmethod
    def _can_run(module: str) -> bool:
        """Return ``True`` if *module* is available as ``python -m <module>`` or on PATH."""
        try:
            import importlib.util
            if importlib.util.find_spec(module) is not None:
                return True
        except (ImportError, ValueError):
            pass
        return shutil.which(module) is not None

    @staticmethod
    def _has_pytest() -> bool:
        """Return ``True`` if pytest is importable (works even when not on PATH)."""
        try:
            import importlib.util
            return importlib.util.find_spec("pytest") is not None
        except (ImportError, ValueError):
            return shutil.which("pytest") is not None

    def get_verification_commands(self) -> list[dict[str, Any]]:
        """Return an ordered list of verification commands for this project."""
        caps = self.detect_project_type()
        commands: list[dict[str, Any]] = []

        if caps.get("has_python"):
            # syntax check – always available with Python
            commands.append({
                "name": "syntax_check",
                "cmd": "python -m py_compile {changed_files}",
                "timeout": 30,
            })

            if self._can_run("ruff"):
                commands.append({
                    "name": "lint",
                    "cmd": "python -m ruff check .",
                    "timeout": 60,
                })

            if self._can_run("mypy"):
                commands.append({
                    "name": "type_check",
                    "cmd": "python -m mypy {changed_files} --ignore-missing-imports",
                    "timeout": 120,
                })

            if caps.get("has_pytest") and self._has_pytest():
                commands.append({
                    "name": "tests",
                    "cmd": "python -m pytest --tb=short -q",
                    "timeout": 300,
                })

        if caps.get("has_go") and shutil.which("go"):
            commands.append({
                "name": "go_test",
                "cmd": "go test ./...",
                "timeout": 300,
            })

        if caps.get("has_npm") and shutil.which("npm"):
            commands.append({
                "name": "npm_test",
                "cmd": "npm test",
                "timeout": 300,
            })

        return commands

    # ------------------------------------------------------------------
    # Running a single command
    # ------------------------------------------------------------------

    def run_command(self, cmd: str, timeout: int = 60) -> VerificationResult:
        """Execute *cmd* in a subprocess and return a ``VerificationResult``."""
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            combined = proc.stdout + proc.stderr
            error_count = len(re.findall(r"(?i)\berror:", combined))
            error_count += len(re.findall(r"\bFAILED\b", combined))
            error_count += len(re.findall(r"(?<!\w)Error(?!\w)", combined))
            warning_count = len(re.findall(r"(?i)\bwarning:", combined))

            return VerificationResult(
                success=proc.returncode == 0,
                command=cmd,
                stdout=proc.stdout,
                stderr=proc.stderr,
                exit_code=proc.returncode,
                error_count=error_count,
                warning_count=warning_count,
            )
        except subprocess.TimeoutExpired:
            return VerificationResult(
                success=False,
                command=cmd,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                exit_code=-1,
                error_count=1,
                warning_count=0,
            )
        except OSError as exc:
            return VerificationResult(
                success=False,
                command=cmd,
                stdout="",
                stderr=str(exc),
                exit_code=-1,
                error_count=1,
                warning_count=0,
            )

    # ------------------------------------------------------------------
    # Full verification run
    # ------------------------------------------------------------------

    def run_verification(
        self, changed_files: list[str] | None = None,
    ) -> list[VerificationResult]:
        """Run all applicable verification commands and return results.

        Stops early when the syntax check fails.
        """
        commands = self.get_verification_commands()
        if not commands:
            return []

        if changed_files:
            files_str = " ".join(changed_files)
        else:
            # Discover Python files for commands that need explicit file arguments
            py_files = [
                str(p.relative_to(self.repo_path))
                for p in self.repo_path.rglob("*.py")
                if ".localforge" not in p.parts
            ]
            files_str = " ".join(py_files)

        results: list[VerificationResult] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            for entry in commands:
                cmd = entry["cmd"].replace("{changed_files}", files_str)

                # Skip commands that require file arguments if none found
                if "{changed_files}" in entry["cmd"] and not files_str:
                    continue

                task_id = progress.add_task(f"Running {entry['name']}...", total=None)
                result = self.run_command(cmd, timeout=entry["timeout"])
                results.append(result)
                progress.update(task_id, completed=True)

                # Stop early on syntax failure
                if entry["name"] == "syntax_check" and not result.success:
                    break

        return results

    # ------------------------------------------------------------------
    # Summarise results
    # ------------------------------------------------------------------

    def summarize_results(self, results: list[VerificationResult]) -> dict[str, Any]:
        """Produce a compact summary dict from a list of verification results."""
        failed = [r.command for r in results if not r.success]
        total_errors = sum(r.error_count for r in results)
        total_warnings = sum(r.warning_count for r in results)
        all_passed = len(failed) == 0 and len(results) > 0

        if all_passed:
            summary = "All verification checks passed."
        elif not results:
            summary = "No verification commands were run."
        else:
            summary = f"{len(failed)} command(s) failed with {total_errors} error(s)."

        return {
            "all_passed": all_passed,
            "failed_commands": failed,
            "total_errors": total_errors,
            "total_warnings": total_warnings,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # Error parsing
    # ------------------------------------------------------------------

    def parse_python_errors(self, output: str) -> list[dict[str, Any]]:
        """Parse common Python error formats from combined output.

        Recognised formats:
        - pytest:  ``FAILED tests/test_x.py::test_y - AssertionError: ...``
        - mypy:    ``file.py:10: error: ...``
        - ruff:    ``file.py:10:5: E501 ...``
        """
        errors: list[dict[str, Any]] = []

        # pytest failures
        for m in re.finditer(
            r"FAILED\s+([\w/\\._-]+)::(\S+)\s*-?\s*(.*)", output,
        ):
            errors.append({
                "file": m.group(1),
                "line": None,
                "message": f"{m.group(2)}: {m.group(3)}".strip(),
                "tool": "pytest",
            })

        # mypy errors
        for m in re.finditer(
            r"^([\w/\\._-]+):(\d+):\s*error:\s*(.+)", output, re.MULTILINE,
        ):
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": m.group(3).strip(),
                "tool": "mypy",
            })

        # ruff diagnostics
        for m in re.finditer(
            r"^([\w/\\._-]+):(\d+):\d+:\s*(\S+\s+.+)", output, re.MULTILINE,
        ):
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": m.group(3).strip(),
                "tool": "ruff",
            })

        return errors
