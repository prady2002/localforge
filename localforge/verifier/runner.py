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
        """Return a dict of detected project capabilities across all tech stacks."""
        caps: dict[str, bool] = {}

        # ── Python ───────────────────────────────────────────────
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
        caps["has_python"] = any(self.repo_path.rglob("*.py"))

        # ── JavaScript / TypeScript / Node.js ────────────────────
        caps["has_npm"] = (self.repo_path / "package.json").is_file()
        caps["has_yarn"] = (self.repo_path / "yarn.lock").is_file()
        caps["has_pnpm"] = (self.repo_path / "pnpm-lock.yaml").is_file()
        caps["has_typescript"] = (
            (self.repo_path / "tsconfig.json").is_file()
            or any(self.repo_path.rglob("*.ts"))
        )
        caps["has_javascript"] = any(self.repo_path.rglob("*.js"))

        # Detect test framework from package.json
        caps["has_jest"] = False
        caps["has_vitest"] = False
        caps["has_mocha"] = False
        caps["has_eslint"] = False
        caps["has_prettier"] = False
        caps["has_biome"] = False
        caps["has_next"] = False
        caps["has_react"] = False
        caps["has_vue"] = False
        caps["has_angular"] = False
        caps["has_svelte"] = False
        caps["npm_test_script"] = False
        caps["npm_build_script"] = False
        caps["npm_lint_script"] = False
        if caps["has_npm"]:
            try:
                import json as _json
                pkg_data = _json.loads(
                    (self.repo_path / "package.json").read_text(encoding="utf-8")
                )
                all_deps = {}
                all_deps.update(pkg_data.get("dependencies", {}))
                all_deps.update(pkg_data.get("devDependencies", {}))

                caps["has_jest"] = "jest" in all_deps
                caps["has_vitest"] = "vitest" in all_deps
                caps["has_mocha"] = "mocha" in all_deps
                caps["has_eslint"] = "eslint" in all_deps
                caps["has_prettier"] = "prettier" in all_deps
                caps["has_biome"] = "@biomejs/biome" in all_deps or "biome" in all_deps
                caps["has_next"] = "next" in all_deps
                caps["has_react"] = "react" in all_deps
                caps["has_vue"] = "vue" in all_deps
                caps["has_angular"] = "@angular/core" in all_deps
                caps["has_svelte"] = "svelte" in all_deps

                # Detect available npm scripts
                scripts = pkg_data.get("scripts", {})
                caps["npm_test_script"] = "test" in scripts
                caps["npm_build_script"] = "build" in scripts
                caps["npm_lint_script"] = "lint" in scripts
                # Store the actual scripts for intelligent command selection
                caps["_npm_scripts"] = scripts  # type: ignore[assignment]
            except (OSError, ValueError):
                pass

        # ── Go ───────────────────────────────────────────────────
        caps["has_go"] = (self.repo_path / "go.mod").is_file()

        # ── Rust / Cargo ─────────────────────────────────────────
        caps["has_rust"] = (self.repo_path / "Cargo.toml").is_file()

        # ── Java / Maven / Gradle ────────────────────────────────
        caps["has_maven"] = (self.repo_path / "pom.xml").is_file()
        caps["has_gradle"] = (
            (self.repo_path / "build.gradle").is_file()
            or (self.repo_path / "build.gradle.kts").is_file()
        )
        caps["has_java"] = caps["has_maven"] or caps["has_gradle"] or any(self.repo_path.rglob("*.java"))

        # ── .NET / C# ───────────────────────────────────────────
        caps["has_dotnet"] = (
            any(self.repo_path.glob("*.csproj"))
            or any(self.repo_path.glob("*.sln"))
            or any(self.repo_path.rglob("*.cs"))
        )

        # ── Ruby ─────────────────────────────────────────────────
        caps["has_ruby"] = (self.repo_path / "Gemfile").is_file()
        caps["has_rails"] = caps["has_ruby"] and (self.repo_path / "config" / "routes.rb").is_file()

        # ── PHP / Composer ───────────────────────────────────────
        caps["has_php"] = (self.repo_path / "composer.json").is_file()

        # ── C / C++ / CMake ──────────────────────────────────────
        caps["has_cmake"] = (self.repo_path / "CMakeLists.txt").is_file()
        caps["has_cpp"] = caps["has_cmake"] or any(self.repo_path.rglob("*.cpp")) or any(self.repo_path.rglob("*.c"))

        # ── Swift ────────────────────────────────────────────────
        caps["has_swift"] = (self.repo_path / "Package.swift").is_file()

        # ── Kotlin ───────────────────────────────────────────────
        caps["has_kotlin"] = any(self.repo_path.rglob("*.kt"))

        # ── Docker ───────────────────────────────────────────────
        caps["has_docker"] = (
            (self.repo_path / "Dockerfile").is_file()
            or (self.repo_path / "docker-compose.yml").is_file()
            or (self.repo_path / "docker-compose.yaml").is_file()
        )

        # Makefile
        caps["has_makefile"] = (self.repo_path / "Makefile").is_file()

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

    def get_verification_commands(self, include_lint: bool = False) -> list[dict[str, Any]]:
        """Return an ordered list of verification commands for this project.

        Supports Python, Node.js/TypeScript, Go, Rust, Java, .NET, Ruby,
        PHP, C/C++, Swift, and more. Auto-detects project type.

        Parameters
        ----------
        include_lint:
            When *True*, include linters in the command list.
            When *False* (default), only syntax checks and test runners.
        """
        caps = self.detect_project_type()
        commands: list[dict[str, Any]] = []

        # ── Python ───────────────────────────────────────────────
        if caps.get("has_python"):
            commands.append({
                "name": "python_syntax_check",
                "cmd": "python -m py_compile {changed_files}",
                "timeout": 30,
            })

            if include_lint and self._can_run("ruff"):
                commands.append({
                    "name": "python_lint",
                    "cmd": "python -m ruff check .",
                    "timeout": 60,
                })
            elif include_lint and self._can_run("flake8"):
                commands.append({
                    "name": "python_lint",
                    "cmd": "python -m flake8 .",
                    "timeout": 60,
                })

            if self._can_run("mypy"):
                commands.append({
                    "name": "python_type_check",
                    "cmd": "python -m mypy {changed_files} --ignore-missing-imports",
                    "timeout": 120,
                })

            if caps.get("has_pytest") and self._has_pytest():
                commands.append({
                    "name": "python_tests",
                    "cmd": "python -m pytest --tb=short -q",
                    "timeout": 300,
                })

        # ── JavaScript / TypeScript / Node.js ────────────────────
        if caps.get("has_npm") or caps.get("has_javascript") or caps.get("has_typescript"):
            # Detect package manager for running commands
            pkg_mgr = "npm"
            pkg_runner = "npx"
            if caps.get("has_pnpm"):
                pkg_mgr = "pnpm"
                pkg_runner = "pnpm exec"
            elif caps.get("has_yarn"):
                pkg_mgr = "yarn"
                pkg_runner = "yarn"

            # Use the project's OWN npm scripts when available — this is the
            # most reliable way because the project authors know best how to
            # build/test/lint their specific setup.
            npm_scripts = caps.get("_npm_scripts", {})

            # TypeScript type checking
            if caps.get("has_typescript"):
                # Prefer the project's own type-check script if it exists
                if "typecheck" in npm_scripts or "type-check" in npm_scripts:
                    script_name = "typecheck" if "typecheck" in npm_scripts else "type-check"
                    commands.append({
                        "name": "ts_type_check",
                        "cmd": f"{pkg_mgr} run {script_name}",
                        "timeout": 120,
                    })
                elif shutil.which("npx"):
                    commands.append({
                        "name": "ts_type_check",
                        "cmd": f"{pkg_runner} tsc --noEmit",
                        "timeout": 120,
                    })

            # Linting — prefer project's lint script
            if include_lint:
                if "lint" in npm_scripts:
                    commands.append({
                        "name": "js_lint",
                        "cmd": f"{pkg_mgr} run lint",
                        "timeout": 60,
                    })
                elif caps.get("has_biome") and shutil.which("npx"):
                    commands.append({
                        "name": "js_lint",
                        "cmd": f"{pkg_runner} biome check .",
                        "timeout": 60,
                    })
                elif caps.get("has_eslint") and shutil.which("npx"):
                    commands.append({
                        "name": "js_lint",
                        "cmd": f"{pkg_runner} eslint .",
                        "timeout": 60,
                    })

            # Build check — useful for frontend projects (Next.js, etc.)
            if "build" in npm_scripts:
                # Only run build for framework projects where build = compile check
                if caps.get("has_next") or caps.get("has_angular") or caps.get("has_svelte"):
                    commands.append({
                        "name": "js_build",
                        "cmd": f"{pkg_mgr} run build",
                        "timeout": 300,
                    })

            # Testing — prefer project's test script, else detect framework
            if "test" in npm_scripts:
                commands.append({
                    "name": "js_tests",
                    "cmd": f"{pkg_mgr} test",
                    "timeout": 300,
                })
            elif caps.get("has_vitest") and shutil.which("npx"):
                commands.append({
                    "name": "js_tests",
                    "cmd": f"{pkg_runner} vitest run",
                    "timeout": 300,
                })
            elif caps.get("has_jest") and shutil.which("npx"):
                commands.append({
                    "name": "js_tests",
                    "cmd": f"{pkg_runner} jest --passWithNoTests",
                    "timeout": 300,
                })
            elif caps.get("has_mocha") and shutil.which("npx"):
                commands.append({
                    "name": "js_tests",
                    "cmd": f"{pkg_runner} mocha",
                    "timeout": 300,
                })

        # ── Go ───────────────────────────────────────────────────
        if caps.get("has_go") and shutil.which("go"):
            commands.append({
                "name": "go_build",
                "cmd": "go build ./...",
                "timeout": 120,
            })
            if include_lint and shutil.which("golangci-lint"):
                commands.append({
                    "name": "go_lint",
                    "cmd": "golangci-lint run",
                    "timeout": 120,
                })
            commands.append({
                "name": "go_test",
                "cmd": "go test ./...",
                "timeout": 300,
            })

        # ── Rust / Cargo ─────────────────────────────────────────
        if caps.get("has_rust") and shutil.which("cargo"):
            commands.append({
                "name": "rust_check",
                "cmd": "cargo check",
                "timeout": 120,
            })
            if include_lint:
                commands.append({
                    "name": "rust_lint",
                    "cmd": "cargo clippy -- -D warnings",
                    "timeout": 120,
                })
            commands.append({
                "name": "rust_tests",
                "cmd": "cargo test",
                "timeout": 300,
            })

        # ── Java / Maven ─────────────────────────────────────────
        if caps.get("has_maven") and shutil.which("mvn"):
            commands.append({
                "name": "java_compile",
                "cmd": "mvn compile -q",
                "timeout": 300,
            })
            commands.append({
                "name": "java_tests",
                "cmd": "mvn test -q",
                "timeout": 600,
            })

        # ── Java / Gradle ────────────────────────────────────────
        if caps.get("has_gradle") and not caps.get("has_maven"):
            gradle_cmd = "gradle"
            if (self.repo_path / "gradlew").is_file():
                gradle_cmd = "./gradlew"
            elif (self.repo_path / "gradlew.bat").is_file():
                gradle_cmd = "gradlew.bat"
            commands.append({
                "name": "java_build",
                "cmd": f"{gradle_cmd} build -x test",
                "timeout": 300,
            })
            commands.append({
                "name": "java_tests",
                "cmd": f"{gradle_cmd} test",
                "timeout": 600,
            })

        # ── .NET / C# ───────────────────────────────────────────
        if caps.get("has_dotnet") and shutil.which("dotnet"):
            commands.append({
                "name": "dotnet_build",
                "cmd": "dotnet build --nologo -v q",
                "timeout": 300,
            })
            commands.append({
                "name": "dotnet_tests",
                "cmd": "dotnet test --nologo -v q",
                "timeout": 300,
            })

        # ── Ruby ─────────────────────────────────────────────────
        if caps.get("has_ruby"):
            if shutil.which("bundle"):
                commands.append({
                    "name": "ruby_syntax",
                    "cmd": "ruby -c {changed_files}",
                    "timeout": 30,
                })
                if caps.get("has_rails"):
                    commands.append({
                        "name": "rails_tests",
                        "cmd": "bundle exec rails test",
                        "timeout": 300,
                    })
                elif shutil.which("rspec"):
                    commands.append({
                        "name": "ruby_tests",
                        "cmd": "bundle exec rspec",
                        "timeout": 300,
                    })

        # ── PHP ──────────────────────────────────────────────────
        if caps.get("has_php"):
            if shutil.which("php"):
                commands.append({
                    "name": "php_syntax",
                    "cmd": "php -l {changed_files}",
                    "timeout": 30,
                })
            vendor_bin = self.repo_path / "vendor" / "bin"
            if (vendor_bin / "phpunit").is_file():
                commands.append({
                    "name": "php_tests",
                    "cmd": str(vendor_bin / "phpunit"),
                    "timeout": 300,
                })
            if include_lint and (vendor_bin / "phpstan").is_file():
                commands.append({
                    "name": "php_lint",
                    "cmd": str(vendor_bin / "phpstan") + " analyse",
                    "timeout": 120,
                })

        # ── C / C++ / CMake ──────────────────────────────────────
        if caps.get("has_cmake") and shutil.which("cmake"):
            build_dir = self.repo_path / "build"
            if build_dir.is_dir():
                commands.append({
                    "name": "cmake_build",
                    "cmd": "cmake --build build",
                    "timeout": 300,
                })

        # ── Swift ────────────────────────────────────────────────
        if caps.get("has_swift") and shutil.which("swift"):
            commands.append({
                "name": "swift_build",
                "cmd": "swift build",
                "timeout": 300,
            })
            commands.append({
                "name": "swift_tests",
                "cmd": "swift test",
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
        include_lint: bool = False,
    ) -> list[VerificationResult]:
        """Run all applicable verification commands and return results.

        Stops early when the syntax check fails.
        """
        commands = self.get_verification_commands(include_lint=include_lint)
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

    def parse_errors(self, output: str) -> list[dict[str, Any]]:
        """Parse error formats from all supported languages.

        Recognised formats:
        - pytest:     ``FAILED tests/test_x.py::test_y - AssertionError: ...``
        - mypy:       ``file.py:10: error: ...``
        - ruff:       ``file.py:10:5: E501 ...``
        - TSC:        ``file.ts(10,5): error TS2304: ...``
        - ESLint:     ``file.js:10:5: error ...``
        - Go:         ``./file.go:10:5: ...``
        - Rust:       ``error[E0308]: ... --> file.rs:10:5``
        - GCC/Clang:  ``file.c:10:5: error: ...``
        - Java/Maven: ``[ERROR] file.java:[10,5] ...``
        - .NET:       ``file.cs(10,5): error CS0001: ...``
        - Ruby:       ``file.rb:10: syntax error, ...``
        - PHP:        ``Parse error: ... in file.php on line 10``
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

        # ruff / flake8 / pylint diagnostics
        for m in re.finditer(
            r"^([\w/\\._-]+):(\d+):\d+:\s*(\S+\s+.+)", output, re.MULTILINE,
        ):
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": m.group(3).strip(),
                "tool": "ruff",
            })

        # TypeScript (tsc) errors: file.ts(10,5): error TS2304: ...
        for m in re.finditer(
            r"^([\w/\\._-]+)\((\d+),\d+\):\s*error\s+(\S+):\s*(.+)",
            output, re.MULTILINE,
        ):
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": f"{m.group(3)}: {m.group(4)}".strip(),
                "tool": "tsc",
            })

        # ESLint: file.js:10:5: error ...
        for m in re.finditer(
            r"^([\w/\\._-]+\.(?:js|jsx|ts|tsx|mjs)):(\d+):\d+:\s*(error|warning)\s+(.+)",
            output, re.MULTILINE,
        ):
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": m.group(4).strip(),
                "tool": "eslint",
            })

        # Go errors: ./file.go:10:5: ...
        for m in re.finditer(
            r"^\.?/?([\w/\\._-]+\.go):(\d+):\d+:\s*(.+)",
            output, re.MULTILINE,
        ):
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": m.group(3).strip(),
                "tool": "go",
            })

        # Rust errors: error[E0308]: ... --> src/file.rs:10:5
        for m in re.finditer(
            r"-->\s*([\w/\\._-]+\.rs):(\d+):\d+",
            output, re.MULTILINE,
        ):
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": "Rust compilation error",
                "tool": "rustc",
            })

        # GCC / Clang: file.c:10:5: error: ...
        for m in re.finditer(
            r"^([\w/\\._-]+\.(?:c|cpp|h|hpp|cc|cxx)):(\d+):\d+:\s*(?:error|warning):\s*(.+)",
            output, re.MULTILINE,
        ):
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": m.group(3).strip(),
                "tool": "gcc",
            })

        # Java / Maven: [ERROR] file.java:[10,5] ...
        for m in re.finditer(
            r"\[ERROR\]\s*([\w/\\._-]+\.java):\[(\d+),\d+\]\s*(.+)",
            output, re.MULTILINE,
        ):
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": m.group(3).strip(),
                "tool": "javac",
            })

        # .NET: file.cs(10,5): error CS0001: ...
        for m in re.finditer(
            r"^([\w/\\._-]+\.cs)\((\d+),\d+\):\s*error\s+(\S+):\s*(.+)",
            output, re.MULTILINE,
        ):
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": f"{m.group(3)}: {m.group(4)}".strip(),
                "tool": "dotnet",
            })

        # Ruby: file.rb:10: syntax error, ...
        for m in re.finditer(
            r"^([\w/\\._-]+\.rb):(\d+):\s*(.+)",
            output, re.MULTILINE,
        ):
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": m.group(3).strip(),
                "tool": "ruby",
            })

        # PHP: Parse error: ... in file.php on line 10
        for m in re.finditer(
            r"(?:Parse|Fatal)\s+error:.*?in\s+([\w/\\._-]+\.php)\s+on\s+line\s+(\d+)",
            output, re.MULTILINE | re.IGNORECASE,
        ):
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "message": "PHP parse error",
                "tool": "php",
            })

        return errors
