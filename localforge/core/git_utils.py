"""Git integration utilities for localforge."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _get_repo(repo_path: Path) -> Any:
    """Return a ``git.Repo`` object or ``None`` if not a git repo."""
    try:
        import git  # gitpython

        return git.Repo(repo_path, search_parent_directories=True)
    except Exception:
        return None


def is_git_repo(repo_path: Path) -> bool:
    """Check if the given path is inside a git repository."""
    return _get_repo(repo_path) is not None


def get_changed_files(repo_path: Path) -> list[str]:
    """Return repo-relative paths of files with uncommitted changes."""
    repo = _get_repo(repo_path)
    if repo is None:
        return []

    changed: list[str] = []
    try:
        # Staged changes
        for diff in repo.index.diff("HEAD"):
            if diff.a_path:
                changed.append(diff.a_path)
        # Unstaged changes
        for diff in repo.index.diff(None):
            if diff.a_path:
                changed.append(diff.a_path)
        # Untracked files
        changed.extend(repo.untracked_files)
    except Exception:
        logger.debug("Error getting git changes", exc_info=True)

    return sorted(set(changed))


def create_checkpoint(repo_path: Path, message: str = "localforge checkpoint") -> str | None:
    """Stage all changes and create a git commit.

    Returns the commit SHA or ``None`` if the commit could not be created.
    """
    repo = _get_repo(repo_path)
    if repo is None:
        return None

    try:
        # Only commit if there are changes
        if not repo.is_dirty(untracked_files=True):
            return None

        repo.git.add(A=True)
        commit = repo.index.commit(message)
        logger.info("Created git checkpoint: %s", commit.hexsha[:8])
        return str(commit.hexsha)
    except Exception:
        logger.debug("Failed to create git checkpoint", exc_info=True)
        return None


def get_current_branch(repo_path: Path) -> str:
    """Return the current branch name, or empty string."""
    repo = _get_repo(repo_path)
    if repo is None:
        return ""
    try:
        return str(repo.active_branch.name)
    except Exception:
        return ""


def get_recent_commits(repo_path: Path, count: int = 10) -> list[dict[str, Any]]:
    """Return the most recent commits as dicts with sha, message, author, date."""
    repo = _get_repo(repo_path)
    if repo is None:
        return []

    commits: list[dict[str, Any]] = []
    try:
        for commit in repo.iter_commits(max_count=count):
            commits.append({
                "sha": commit.hexsha[:8],
                "message": commit.message.strip(),
                "author": str(commit.author),
                "date": commit.committed_datetime.isoformat(),
            })
    except Exception:
        logger.debug("Error reading git log", exc_info=True)

    return commits


def git_diff_staged(repo_path: Path) -> str:
    """Return the diff of staged changes."""
    repo = _get_repo(repo_path)
    if repo is None:
        return ""
    try:
        return str(repo.git.diff("--cached"))
    except Exception:
        return ""


def git_diff_working(repo_path: Path) -> str:
    """Return the diff of unstaged working-tree changes."""
    repo = _get_repo(repo_path)
    if repo is None:
        return ""
    try:
        return str(repo.git.diff())
    except Exception:
        return ""
