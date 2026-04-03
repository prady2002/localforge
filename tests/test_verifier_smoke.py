"""Smoke-test for the VerificationRunner."""

from pathlib import Path
from localforge.core.config import LocalForgeConfig
from localforge.verifier import VerificationRunner

cfg = LocalForgeConfig()
runner = VerificationRunner(Path("."), cfg)

# 1. detect_project_type
caps = runner.detect_project_type()
print("Capabilities:", caps)
assert caps["has_python"] is True, "Should detect Python files"

# 2. get_verification_commands
cmds = runner.get_verification_commands()
print("Commands (%d):" % len(cmds))
for c in cmds:
    print("  %s: %s" % (c["name"], c["cmd"]))
assert any(c["name"] == "syntax_check" for c in cmds), "Missing syntax_check"

# 3. run_command (success)
result = runner.run_command("python -c \"print(42)\"", timeout=10)
print("run_command OK: success=%s exit=%d stdout=%r" % (result.success, result.exit_code, result.stdout.strip()))
assert result.success and result.exit_code == 0 and "42" in result.stdout

# 4. run_command (failure)
result2 = runner.run_command("python -c \"raise ValueError(123)\"", timeout=10)
print("run_command FAIL: success=%s exit=%d" % (result2.success, result2.exit_code))
assert not result2.success

# 5. summarize_results
summary = runner.summarize_results([result, result2])
print("Summary:", summary)
assert not summary["all_passed"]
assert len(summary["failed_commands"]) == 1

summary_pass = runner.summarize_results([result])
assert summary_pass["all_passed"]

summary_empty = runner.summarize_results([])
assert not summary_empty["all_passed"]

# 6. parse_python_errors
test_output = (
    "FAILED tests/test_x.py::test_login - AssertionError: expected True\n"
    "src/auth.py:10: error: Incompatible return type\n"
    "src/utils.py:25:3: E501 Line too long\n"
)
errors = runner.parse_python_errors(test_output)
print("Parsed errors (%d):" % len(errors))
for e in errors:
    print("  ", e)
assert len(errors) == 3, "Expected 3 errors, got %d" % len(errors)
assert errors[0]["tool"] == "pytest"
assert errors[1]["tool"] == "mypy" and errors[1]["line"] == 10
assert errors[2]["tool"] == "ruff" and errors[2]["line"] == 25

# 7. run_verification (real, against this repo)
results = runner.run_verification(changed_files=["localforge/core/config.py"])
print("run_verification: %d results" % len(results))
for r in results:
    print("  cmd=%r success=%s" % (r.command, r.success))

print()
print("ALL TESTS PASSED")
