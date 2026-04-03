"""Shared fixtures for the localforge test suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from localforge.core.config import LocalForgeConfig


# ---------------------------------------------------------------------------
# tmp_repo — a temporary directory with a fake Python web-app structure
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Create a temp directory with 5+ realistic Python web-app files."""

    # 1. app/main.py — FastAPI entry point
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "__init__.py").write_text("", encoding="utf-8")
    (app_dir / "main.py").write_text(
        '''\
"""Main application entry point."""

from app.routes import router
from app.database import get_db


def create_app():
    """Create and configure the application."""
    app = {"router": router, "db": get_db()}
    return app


if __name__ == "__main__":
    app = create_app()
    print("Server running on port 8000")
''',
        encoding="utf-8",
    )

    # 2. app/routes.py — request handlers
    (app_dir / "routes.py").write_text(
        '''\
"""HTTP route handlers."""


def router(request):
    """Route incoming requests to handlers."""
    path = request.get("path", "/")
    if path == "/":
        return {"status": 200, "body": "Welcome"}
    if path == "/users":
        return list_users(request)
    if path == "/login":
        return login(request)
    return {"status": 404, "body": "Not Found"}


def list_users(request):
    """Return all users."""
    return {"status": 200, "body": []}


def login(request):
    """Authenticate a user."""
    username = request.get("username", "")
    password = request.get("password", "")
    if username == "admin" and password == "secret":
        return {"status": 200, "body": {"token": "abc123"}}
    return {"status": 401, "body": "Unauthorized"}
''',
        encoding="utf-8",
    )

    # 3. app/database.py — database helper
    (app_dir / "database.py").write_text(
        '''\
"""Database connection utilities."""


class Database:
    """Simple in-memory database."""

    def __init__(self):
        self._data: dict = {}

    def get(self, key: str):
        return self._data.get(key)

    def set(self, key: str, value):
        self._data[key] = value

    def delete(self, key: str):
        self._data.pop(key, None)


_db_instance = None


def get_db() -> Database:
    """Return the singleton database instance."""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance
''',
        encoding="utf-8",
    )

    # 4. app/models.py — data models
    (app_dir / "models.py").write_text(
        '''\
"""Domain models for the application."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class User:
    """Represents an application user."""
    id: int
    username: str
    email: str
    is_active: bool = True


@dataclass
class Session:
    """Represents a user session."""
    token: str
    user_id: int
    expires_at: Optional[str] = None
    metadata: dict = field(default_factory=dict)
''',
        encoding="utf-8",
    )

    # 5. app/utils.py — miscellaneous helpers
    (app_dir / "utils.py").write_text(
        '''\
"""Utility functions."""

import hashlib
import re


def hash_password(password: str) -> str:
    """Return a SHA-256 hex digest of *password*."""
    return hashlib.sha256(password.encode()).hexdigest()


def validate_email(email: str) -> bool:
    """Return True if *email* looks like a valid address."""
    pattern = r"^[\\w.+-]+@[\\w-]+\\.[\\w.]+$"
    return bool(re.match(pattern, email))


def slugify(text: str) -> str:
    """Convert *text* to a URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\\w\\s-]", "", text)
    return re.sub(r"[\\s_-]+", "-", text).strip("-")
''',
        encoding="utf-8",
    )

    # 6. tests/test_app.py — sample test file
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_app.py").write_text(
        '''\
"""Tests for the application."""


def test_create_app():
    from app.main import create_app
    app = create_app()
    assert "router" in app


def test_login_success():
    from app.routes import login
    result = login({"username": "admin", "password": "secret"})
    assert result["status"] == 200
''',
        encoding="utf-8",
    )

    # 7. pyproject.toml
    (tmp_path / "pyproject.toml").write_text(
        """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "fake-web-app"
version = "0.1.0"

[tool.pytest.ini_options]
testpaths = ["tests"]
""",
        encoding="utf-8",
    )

    return tmp_path


# ---------------------------------------------------------------------------
# mock_config — a LocalForgeConfig that never touches real disk/env
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_config() -> LocalForgeConfig:
    """Return a LocalForgeConfig with safe defaults for testing."""
    return LocalForgeConfig(
        model_name="qwen2.5-coder:7b",
        ollama_base_url="http://localhost:11434",
        max_context_tokens=4096,
        max_iterations=10,
        repo_path=".",
        index_db_path=":memory:",
        auto_approve=True,
        dry_run=False,
    )


# ---------------------------------------------------------------------------
# mock_ollama_response — a reusable factory for mocked LLM replies
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_ollama_response() -> dict[str, Any]:
    """Return a dict of canned structured JSON responses keyed by agent role."""
    return {
        "analyzer": json.dumps({
            "understanding": "The task involves fixing a bug in the login endpoint.",
            "key_files": ["app/routes.py", "app/main.py"],
            "complexity": "simple",
            "approach": "Modify the login function to handle edge cases.",
            "risks": ["Regression in authentication flow"],
            "needs_more_context": False,
            "additional_context_queries": [],
        }),
        "planner": json.dumps({
            "reasoning": "Single file change needed.",
            "estimated_complexity": "simple",
            "steps": [
                {
                    "step_id": 1,
                    "description": "Fix login validation in routes.py",
                    "files_involved": ["app/routes.py"],
                    "operation": "MODIFY",
                },
            ],
        }),
        "coder": json.dumps({
            "file_path": "app/routes.py",
            "operation": "MODIFY",
            "search_block": 'if username == "admin" and password == "secret":',
            "replace_block": 'if username and password and username == "admin" and password == "secret":',
            "description": "Add null checks before comparing credentials.",
        }),
        "verifier": json.dumps({
            "passed": True,
            "error_summary": "",
            "details": "All tests pass.",
            "recommendation": "proceed",
        }),
        "reflector": json.dumps({
            "root_cause": "Missing null check.",
            "should_skip": False,
            "skip_reason": "",
            "specific_instructions": "Ensure username/password are non-empty before comparison.",
            "alternative_approach": "",
        }),
        "summarizer": json.dumps({
            "summary": "Fixed login validation by adding null checks.",
            "files_changed": ["app/routes.py"],
            "tests_status": "all passing",
            "remaining_issues": [],
        }),
    }
