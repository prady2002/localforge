"""Tests for localforge.index.indexer — RepositoryIndexer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from localforge.core.config import LocalForgeConfig
from localforge.index.indexer import RepositoryIndexer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_indexer(tmp_path: Path, config: LocalForgeConfig) -> RepositoryIndexer:
    db = tmp_path / ".localforge" / "index.db"
    return RepositoryIndexer(tmp_path, db, config)


# ---------------------------------------------------------------------------
# test_initialize_db
# ---------------------------------------------------------------------------


def test_initialize_db(tmp_repo: Path, mock_config: LocalForgeConfig) -> None:
    """DB initialisation must create files, chunks, chunks_fts, and symbols tables."""
    idx = _make_indexer(tmp_repo, mock_config)
    idx.initialize_db()

    conn = sqlite3.connect(str(idx.db_path))
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    }
    conn.close()

    assert "files" in tables
    assert "chunks" in tables
    assert "chunks_fts" in tables
    assert "symbols" in tables
    idx.close()


# ---------------------------------------------------------------------------
# test_detect_language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suffix, expected",
    [
        (".py", "python"),
        (".js", "javascript"),
        (".ts", "typescript"),
        (".tsx", "typescriptreact"),
        (".go", "go"),
        (".rs", "rust"),
        (".java", "java"),
        (".cpp", "cpp"),
        (".rb", "ruby"),
        (".swift", "swift"),
    ],
)
def test_detect_language(suffix: str, expected: str) -> None:
    """detect_language should map 10 common extensions correctly."""
    assert RepositoryIndexer.detect_language(Path(f"file{suffix}")) == expected


def test_detect_language_unknown() -> None:
    assert RepositoryIndexer.detect_language(Path("file.xyz")) == "unknown"


# ---------------------------------------------------------------------------
# test_should_index
# ---------------------------------------------------------------------------


def test_should_index_excludes_node_modules(tmp_repo: Path, mock_config: LocalForgeConfig) -> None:
    """Files under node_modules must be excluded."""
    idx = _make_indexer(tmp_repo, mock_config)
    nm = tmp_repo / "node_modules" / "pkg" / "index.js"
    nm.parent.mkdir(parents=True)
    nm.write_text("module.exports = {};", encoding="utf-8")
    assert idx.should_index(nm) is False
    idx.close()


def test_should_index_excludes_git(tmp_repo: Path, mock_config: LocalForgeConfig) -> None:
    """Files under .git must be excluded."""
    idx = _make_indexer(tmp_repo, mock_config)
    gitobj = tmp_repo / ".git" / "objects" / "ab" / "cdef"
    gitobj.parent.mkdir(parents=True)
    gitobj.write_bytes(b"\x00binary")
    assert idx.should_index(gitobj) is False
    idx.close()


def test_should_index_excludes_pycache(tmp_repo: Path, mock_config: LocalForgeConfig) -> None:
    idx = _make_indexer(tmp_repo, mock_config)
    cached = tmp_repo / "__pycache__" / "foo.cpython-311.pyc"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"\x00\x00")
    assert idx.should_index(cached) is False
    idx.close()


def test_should_index_accepts_normal_file(tmp_repo: Path, mock_config: LocalForgeConfig) -> None:
    idx = _make_indexer(tmp_repo, mock_config)
    normal = tmp_repo / "app" / "main.py"
    assert idx.should_index(normal) is True
    idx.close()


# ---------------------------------------------------------------------------
# test_chunk_file
# ---------------------------------------------------------------------------


def test_chunk_file_small_file() -> None:
    """A file shorter than target_size (50 lines) should produce exactly one chunk."""
    content = "line\n" * 20
    chunks = RepositoryIndexer.chunk_file(Path("small.py"), content)
    assert len(chunks) == 1
    assert chunks[0]["start_line"] == 1
    assert chunks[0]["end_line"] == 20


def test_chunk_file_large_file_produces_overlapping_chunks() -> None:
    """A file of 120 lines should produce multiple overlapping chunks."""
    lines = [f"x = {i}\n" for i in range(120)]
    content = "".join(lines)
    chunks = RepositoryIndexer.chunk_file(Path("big.py"), content)

    assert len(chunks) > 1

    # Verify overlapping: second chunk starts before first chunk ends
    if len(chunks) >= 2:
        assert chunks[1]["start_line"] < chunks[0]["end_line"]


def test_chunk_file_tokens_estimated() -> None:
    content = "a = 1\n" * 10
    chunks = RepositoryIndexer.chunk_file(Path("tok.py"), content)
    assert all(ch["tokens"] > 0 for ch in chunks)


def test_chunk_file_empty() -> None:
    chunks = RepositoryIndexer.chunk_file(Path("empty.py"), "")
    assert chunks == []


# ---------------------------------------------------------------------------
# test_index_file
# ---------------------------------------------------------------------------


def test_index_file(tmp_repo: Path, mock_config: LocalForgeConfig) -> None:
    """Indexing a real Python file should produce DB entries in files, chunks, and symbols."""
    idx = _make_indexer(tmp_repo, mock_config)
    idx.initialize_db()

    target = tmp_repo / "app" / "routes.py"
    result = idx.index_file(target)
    assert result is True

    conn = idx._get_conn()
    file_rows = conn.execute("SELECT * FROM files").fetchall()
    chunk_rows = conn.execute("SELECT * FROM chunks").fetchall()
    symbol_rows = conn.execute("SELECT * FROM symbols").fetchall()

    assert len(file_rows) == 1
    assert file_rows[0]["relative_path"] == "app/routes.py"
    assert len(chunk_rows) >= 1
    # routes.py has multiple def statements → symbols expected
    assert len(symbol_rows) >= 1
    idx.close()


# ---------------------------------------------------------------------------
# test_skip_unchanged
# ---------------------------------------------------------------------------


def test_skip_unchanged(tmp_repo: Path, mock_config: LocalForgeConfig) -> None:
    """Indexing the same unchanged file twice must skip the second call."""
    idx = _make_indexer(tmp_repo, mock_config)
    idx.initialize_db()

    target = tmp_repo / "app" / "main.py"

    first = idx.index_file(target)
    assert first is True

    second = idx.index_file(target)
    assert second is False

    idx.close()


# ---------------------------------------------------------------------------
# test_index_repository
# ---------------------------------------------------------------------------


def test_index_repository(tmp_repo: Path, mock_config: LocalForgeConfig) -> None:
    """Full repository index should report correct stats."""
    idx = _make_indexer(tmp_repo, mock_config)
    stats = idx.index_repository()

    assert stats["total_files"] >= 5  # we created at least 5 .py files
    assert stats["indexed"] >= 5
    assert stats["errors"] == 0
    assert stats["duration_seconds"] >= 0

    # DB should have rows
    conn = idx._get_conn()
    file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert file_count >= 5
    assert chunk_count >= 5

    idx.close()
