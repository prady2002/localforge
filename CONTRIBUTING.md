# Contributing to LocalForge

Thanks for your interest in contributing! LocalForge is an open-source project
and we welcome contributions of all kinds.

## Getting Started

1. **Fork & clone** the repository:

   ```bash
   git clone https://github.com/localforge/localforge.git
   cd localforge
   ```

2. **Install in development mode** with dev dependencies:

   ```bash
   pip install -e ".[dev]"
   ```

3. **Verify** the setup:

   ```bash
   pytest
   ruff check .
   ```

## Development Workflow

1. Create a feature branch from `main`:

   ```bash
   git checkout -b feature/my-feature
   ```

2. Make your changes. Follow the existing code style (PEP 8, 100 char lines,
   type hints on public APIs).

3. Write or update tests for any changed functionality.

4. Run the full check suite:

   ```bash
   # Lint
   ruff check .

   # Type check
   mypy localforge/

   # Tests
   pytest -v
   ```

5. Commit with a clear, descriptive message:

   ```bash
   git commit -m "feat: add support for custom retrieval strategies"
   ```

6. Push and open a Pull Request against `main`.

## Code Style

- **Formatter:** Black (line length 100).
- **Linter:** Ruff with the rule set defined in `pyproject.toml`.
- **Type checker:** mypy in strict mode.
- Python 3.11+ features are fine (use `from __future__ import annotations`).
- Use `pathlib.Path` over `os.path`.
- Use structured logging, not `print()`.

## Project Structure

```
localforge/
  agent/         # Multi-agent system (orchestrator, specialist agents)
  cli/           # Typer CLI commands and display helpers
  context_manager/  # Token budget management and context assembly
  core/          # Config, models, Ollama client, prompt templates
  index/         # Repository indexing (SQLite) and search
  patching/      # File patching with backup and validation
  retrieval/     # Context retrieval and ranking
  verifier/      # Verification runner (lint, test, type-check)
tests/           # All tests (pytest)
```

## Writing Tests

- Place tests in `tests/test_<module>.py`.
- Use `pytest` fixtures from `tests/conftest.py`.
- Mock external dependencies (Ollama API, filesystem writes) in unit tests.
- Use `pytest-asyncio` for async tests — the project uses `asyncio_mode = "auto"`.
- Aim for tests that run fast and don't require a running Ollama instance.

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` — new feature
- `fix:` — bug fix
- `docs:` — documentation only
- `test:` — adding or updating tests
- `refactor:` — code change that neither fixes a bug nor adds a feature
- `chore:` — build process, CI, dependency updates

## Pull Request Guidelines

- Keep PRs focused — one feature or fix per PR.
- Include a clear description of **what** changed and **why**.
- All CI checks (pytest, ruff, mypy) must pass.
- Add tests for new functionality.
- Update documentation if you add or change CLI commands or config fields.

## Reporting Issues

- Use GitHub Issues.
- Include: Python version, OS, Ollama version, model used, and steps to
  reproduce.
- Paste the full error traceback if applicable.

## License

By contributing, you agree that your contributions will be licensed under the
MIT License.
