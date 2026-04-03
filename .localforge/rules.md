# LocalForge Rules
#
# This file tells the agent about project-specific conventions and constraints.
# Everything here is included in the agent's context for every task, so keep it
# concise and actionable.

## Coding Style

- Follow PEP 8 and use `ruff` for linting.
- Maximum line length: 100 characters.
- Use type hints on all public function signatures.
- Prefer `pathlib.Path` over `os.path`.
- Use f-strings for string formatting (no `.format()` or `%`).

## Forbidden Patterns

- Do not use `print()` for logging — use the `logging` module.
- Do not use `import *` anywhere.
- Do not use mutable default arguments (e.g., `def f(x=[])`).
- Do not catch bare `except:` — always specify the exception type.
- Do not use `os.system()` or `subprocess.call()` with `shell=True` for
  untrusted input.

## Test Requirements

- Every new public function must have at least one corresponding test.
- Tests live in `tests/` and follow the naming convention `test_<module>.py`.
- Use `pytest` as the test runner.
- Use `pytest-asyncio` for async tests (mode: `auto`).
- Mock external dependencies (Ollama, filesystem) in unit tests.
- Tests must pass before any PR is merged.

## File Naming

- Python modules: `snake_case.py`
- Test files: `test_<module_name>.py`
- Configuration files: lowercase with hyphens or underscores.
- No spaces or special characters in filenames.

## Documentation

- Add docstrings to all public classes and functions.
- Use Google-style or NumPy-style docstrings consistently.
- Update the README when adding new CLI commands or config fields.
