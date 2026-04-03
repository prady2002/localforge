"""Repository indexing engine for localforge.

Walks a repository, chunks source files, and stores the results in a local
SQLite database so that the retrieval and search layers can query them
efficiently.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pathspec
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from localforge.core.config import LocalForgeConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language detection map
# ---------------------------------------------------------------------------

_EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".jsx": "javascriptreact",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "shell",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".md": "markdown",
}

# Directories that should never be indexed.
_SKIP_DIRS: set[str] = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".localforge",
}

# Maximum file size we will attempt to index (1 MB).
_MAX_FILE_SIZE: int = 1_048_576

# Keywords that indicate the start of a logical block in most languages.
_BLOCK_KEYWORDS: set[str] = {
    "def ",
    "class ",
    "func ",
    "function ",
    "fn ",
    "pub fn ",
    "async def ",
    "async function ",
}


class RepositoryIndexer:
    """Builds and maintains a SQLite-backed index of a source repository."""

    def __init__(self, repo_path: Path, db_path: Path, config: LocalForgeConfig) -> None:
        """Initialise the indexer.

        Parameters
        ----------
        repo_path:
            Absolute path to the repository root.
        db_path:
            Path to the SQLite database file.
        config:
            Application configuration.
        """
        self.repo_path = repo_path.resolve()
        self.db_path = db_path.resolve()
        self.config = config
        self._conn: sqlite3.Connection | None = None
        self._gitignore_spec: pathspec.PathSpec | None = None

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Return the current connection, creating one if needed."""
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def initialize_db(self) -> None:
        """Create the index tables if they do not already exist.

        Tables
        ------
        files
            One row per indexed file.
        chunks
            One row per text chunk extracted from a file.
        chunks_fts
            FTS5 virtual table for full-text search over chunk content.
        symbols
            Extracted symbol names (functions, classes, etc.).
        """
        conn = self._get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                path         TEXT    NOT NULL,
                relative_path TEXT   NOT NULL,
                size         INTEGER NOT NULL,
                mtime        REAL    NOT NULL,
                language     TEXT    NOT NULL DEFAULT '',
                indexed_at   REAL    NOT NULL,
                hash         TEXT    NOT NULL,
                UNIQUE(relative_path)
            );

            CREATE TABLE IF NOT EXISTS chunks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                start_line   INTEGER NOT NULL,
                end_line     INTEGER NOT NULL,
                content      TEXT    NOT NULL,
                content_hash TEXT    NOT NULL,
                tokens       INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS symbols (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                name    TEXT    NOT NULL,
                kind    TEXT    NOT NULL DEFAULT '',
                line    INTEGER NOT NULL,
                scope   TEXT    NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_files_relative_path ON files(relative_path);
            CREATE INDEX IF NOT EXISTS idx_chunks_file_id ON chunks(file_id);
            CREATE INDEX IF NOT EXISTS idx_symbols_file_id ON symbols(file_id);
            CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
            """
        )

        # FTS5 virtual table — CREATE VIRTUAL TABLE IF NOT EXISTS is supported.
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
            USING fts5(content, content='chunks', content_rowid='id')
            """
        )

        conn.commit()
        logger.info("Index database initialised at %s", self.db_path)

    # ------------------------------------------------------------------
    # Language detection
    # ------------------------------------------------------------------

    @staticmethod
    def detect_language(path: Path) -> str:
        """Return the language identifier for *path* based on its extension.

        Parameters
        ----------
        path:
            File path to inspect.

        Returns
        -------
        str
            Language name (e.g. ``"python"``), or ``"unknown"`` if the
            extension is not recognised.
        """
        return _EXTENSION_LANGUAGE.get(path.suffix.lower(), "unknown")

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _load_gitignore(self) -> pathspec.PathSpec:
        """Parse the repository ``.gitignore`` into a *pathspec* matcher."""
        if self._gitignore_spec is not None:
            return self._gitignore_spec

        gitignore_path = self.repo_path / ".gitignore"
        patterns: list[str] = []
        if gitignore_path.is_file():
            try:
                patterns = gitignore_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                logger.debug("Could not read .gitignore")

        self._gitignore_spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        return self._gitignore_spec

    @staticmethod
    def _is_binary(path: Path) -> bool:
        """Heuristic binary-file check (look for null bytes in the first 8 KB)."""
        try:
            with open(path, "rb") as fh:
                chunk = fh.read(8192)
            return b"\x00" in chunk
        except OSError:
            return True

    def should_index(self, path: Path) -> bool:
        """Decide whether *path* should be included in the index.

        A file is excluded when:
        * It resides inside a skip-listed directory.
        * It is a binary file.
        * Its size exceeds 1 MB.
        * It matches a ``.gitignore`` pattern.

        Parameters
        ----------
        path:
            Absolute path to the candidate file.

        Returns
        -------
        bool
        """
        try:
            rel = path.resolve().relative_to(self.repo_path)
        except ValueError:
            return False

        # Check each part of the relative path against skip dirs.
        for part in rel.parts:
            if part in _SKIP_DIRS:
                return False

        # Size check.
        try:
            if path.stat().st_size > _MAX_FILE_SIZE:
                return False
        except OSError:
            return False

        # Binary check.
        if self._is_binary(path):
            return False

        # .gitignore check.
        spec = self._load_gitignore()
        return not spec.match_file(str(rel.as_posix()))

    # ------------------------------------------------------------------
    # Chunking
    # ------------------------------------------------------------------

    @staticmethod
    def chunk_file(file_path: Path, content: str) -> list[dict[str, Any]]:
        """Split *content* into overlapping chunks of roughly 50 lines.

        The chunker avoids splitting in the middle of a function / class
        definition by looking for block-start keywords at the planned
        boundary and adjusting forward to keep the block intact.

        Parameters
        ----------
        file_path:
            Used only for logging; not read from disk here.
        content:
            The full text of the file.

        Returns
        -------
        list[dict]
            Each dict has keys ``start_line``, ``end_line``, ``content``,
            and ``tokens`` (estimated as ``len(content) // 4``).
        """
        lines = content.splitlines(keepends=True)
        total = len(lines)
        if total == 0:
            return []

        target_size = 50
        overlap = 10
        chunks: list[dict[str, Any]] = []
        start = 0

        while start < total:
            end = min(start + target_size, total)

            # If we are not at the very end, try to avoid splitting mid-block.
            if end < total:
                # Look ahead up to 15 lines for a block keyword.
                best_end = end
                for probe in range(end, min(end + 15, total)):
                    stripped = lines[probe].lstrip()
                    if any(stripped.startswith(kw) for kw in _BLOCK_KEYWORDS):
                        best_end = probe
                        break
                end = best_end

            chunk_lines = lines[start:end]
            chunk_content = "".join(chunk_lines)
            chunks.append(
                {
                    "start_line": start + 1,  # 1-based
                    "end_line": end,  # inclusive
                    "content": chunk_content,
                    "tokens": max(1, len(chunk_content) // 4),
                }
            )

            if end >= total:
                break
            start = max(end - overlap, start + 1)

        return chunks

    # ------------------------------------------------------------------
    # File hashing
    # ------------------------------------------------------------------

    @staticmethod
    def _file_hash(content: bytes) -> str:
        """Return the hex MD5 digest of *content*."""
        return hashlib.md5(content).hexdigest()  # noqa: S324

    # ------------------------------------------------------------------
    # Single-file indexing
    # ------------------------------------------------------------------

    def index_file(self, path: Path) -> bool:
        """Index a single file into the database.

        If the file content hash is unchanged since the last index run the
        file is silently skipped.

        Parameters
        ----------
        path:
            Absolute path to the file.

        Returns
        -------
        bool
            ``True`` if the file was (re-)indexed, ``False`` if skipped.
        """
        conn = self._get_conn()
        try:
            rel = path.resolve().relative_to(self.repo_path)
        except ValueError:
            logger.warning("Path %s is outside repo root – skipped", path)
            return False

        rel_posix = rel.as_posix()

        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.warning("Could not read %s: %s", path, exc)
            return False

        file_hash = self._file_hash(raw)

        # Check whether the file is unchanged.
        row = conn.execute(
            "SELECT id, hash FROM files WHERE relative_path = ?", (rel_posix,)
        ).fetchone()

        if row and row["hash"] == file_hash:
            return False

        content = raw.decode("utf-8", errors="replace")
        language = self.detect_language(path)
        stat = path.stat()
        now = time.time()

        # Remove old data for this file if it existed.
        if row:
            file_id = row["id"]
            conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
            conn.execute(
                """
                UPDATE files
                   SET path = ?, size = ?, mtime = ?, language = ?,
                       indexed_at = ?, hash = ?
                 WHERE id = ?
                """,
                (str(path), stat.st_size, stat.st_mtime, language, now, file_hash, file_id),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO files (path, relative_path, size, mtime, language, indexed_at, hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(path), rel_posix, stat.st_size, stat.st_mtime, language, now, file_hash),
            )
            file_id = cur.lastrowid

        # Chunk and insert.
        chunks = self.chunk_file(path, content)
        for ch in chunks:
            content_hash = hashlib.md5(ch["content"].encode()).hexdigest()  # noqa: S324
            conn.execute(
                """
                INSERT INTO chunks (file_id, start_line, end_line, content, content_hash, tokens)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (file_id, ch["start_line"], ch["end_line"], ch["content"], content_hash, ch["tokens"]),
            )

        # Extract basic symbols (functions / classes) for supported languages.
        self._extract_symbols(file_id, content, language, conn)

        # Rebuild FTS index for the affected rows.
        self._rebuild_fts(conn)

        conn.commit()
        return True

    # ------------------------------------------------------------------
    # Simple symbol extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_symbols(
        file_id: int, content: str, language: str, conn: sqlite3.Connection
    ) -> None:
        """Extract top-level symbol definitions and store them.

        This intentionally uses simple heuristic line scanning rather than a
        full parser — accurate enough for search/navigation purposes.
        """
        lines = content.splitlines()
        for lineno, line in enumerate(lines, start=1):
            stripped = line.lstrip()

            # Python
            if language == "python":
                if stripped.startswith("def "):
                    name = stripped[4:].split("(", 1)[0].strip()
                    scope = "module" if not line[0].isspace() else "local"
                    conn.execute(
                        "INSERT INTO symbols (file_id, name, kind, line, scope) VALUES (?,?,?,?,?)",
                        (file_id, name, "function", lineno, scope),
                    )
                elif stripped.startswith("class "):
                    name = stripped[6:].split("(", 1)[0].split(":", 1)[0].strip()
                    conn.execute(
                        "INSERT INTO symbols (file_id, name, kind, line, scope) VALUES (?,?,?,?,?)",
                        (file_id, name, "class", lineno, "module"),
                    )

            # JS / TS / Go / Rust / Java / C# / etc.
            elif language in {
                "javascript", "typescript", "typescriptreact", "javascriptreact",
                "go", "rust", "java", "csharp", "kotlin", "scala", "swift", "php",
                "c", "cpp", "ruby", "shell",
            }:
                if stripped.startswith("function "):
                    name = stripped[9:].split("(", 1)[0].strip()
                    conn.execute(
                        "INSERT INTO symbols (file_id, name, kind, line, scope) VALUES (?,?,?,?,?)",
                        (file_id, name, "function", lineno, "module"),
                    )
                elif stripped.startswith("class "):
                    name = stripped[6:].split("(", 1)[0].split("{", 1)[0].split(":", 1)[0].strip()
                    conn.execute(
                        "INSERT INTO symbols (file_id, name, kind, line, scope) VALUES (?,?,?,?,?)",
                        (file_id, name, "class", lineno, "module"),
                    )
                elif stripped.startswith("func ") or stripped.startswith("fn "):
                    prefix_len = 5 if stripped.startswith("func ") else 3
                    name = stripped[prefix_len:].split("(", 1)[0].strip()
                    conn.execute(
                        "INSERT INTO symbols (file_id, name, kind, line, scope) VALUES (?,?,?,?,?)",
                        (file_id, name, "function", lineno, "module"),
                    )
                elif stripped.startswith("pub fn "):
                    name = stripped[7:].split("(", 1)[0].strip()
                    conn.execute(
                        "INSERT INTO symbols (file_id, name, kind, line, scope) VALUES (?,?,?,?,?)",
                        (file_id, name, "function", lineno, "module"),
                    )
                elif stripped.startswith("def "):
                    name = stripped[4:].split("(", 1)[0].strip()
                    conn.execute(
                        "INSERT INTO symbols (file_id, name, kind, line, scope) VALUES (?,?,?,?,?)",
                        (file_id, name, "function", lineno, "module"),
                    )

    # ------------------------------------------------------------------
    # FTS rebuild helper
    # ------------------------------------------------------------------

    @staticmethod
    def _rebuild_fts(conn: sqlite3.Connection) -> None:
        """Rebuild the FTS5 index from the chunks table."""
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")

    # ------------------------------------------------------------------
    # Full-repository indexing
    # ------------------------------------------------------------------

    def index_repository(self, force: bool = False) -> dict[str, Any]:
        """Walk the repository and index every eligible file.

        Parameters
        ----------
        force:
            When ``True``, re-index every file regardless of its hash.

        Returns
        -------
        dict
            Statistics with keys ``total_files``, ``indexed``, ``skipped``,
            ``errors``, and ``duration_seconds``.
        """
        self.initialize_db()

        if force:
            conn = self._get_conn()
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM symbols")
            conn.execute("DELETE FROM files")
            conn.commit()

        start_time = time.time()
        stats: dict[str, Any] = {
            "total_files": 0,
            "indexed": 0,
            "skipped": 0,
            "errors": 0,
            "duration_seconds": 0.0,
        }

        # Collect eligible files first so we can report progress.
        eligible: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.repo_path):
            # Prune skip dirs in-place so os.walk doesn't descend into them.
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                if self.should_index(fpath):
                    eligible.append(fpath)

        stats["total_files"] = len(eligible)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
        ) as progress:
            task_id = progress.add_task("Indexing files", total=len(eligible))
            for fpath in eligible:
                try:
                    indexed = self.index_file(fpath)
                    if indexed:
                        stats["indexed"] += 1
                    else:
                        stats["skipped"] += 1
                except Exception:
                    logger.exception("Error indexing %s", fpath)
                    stats["errors"] += 1
                progress.advance(task_id)

        stats["duration_seconds"] = round(time.time() - start_time, 3)
        logger.info(
            "Indexing complete: %d indexed, %d skipped, %d errors in %.2fs",
            stats["indexed"],
            stats["skipped"],
            stats["errors"],
            stats["duration_seconds"],
        )
        return stats

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return summary statistics about the current index.

        Returns
        -------
        dict
            Keys: ``total_files``, ``total_chunks``, ``total_symbols``,
            ``languages`` (dict mapping language → count).
        """
        conn = self._get_conn()
        total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        total_symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]

        lang_rows = conn.execute(
            "SELECT language, COUNT(*) AS cnt FROM files GROUP BY language ORDER BY cnt DESC"
        ).fetchall()
        languages = {row["language"]: row["cnt"] for row in lang_rows}

        return {
            "total_files": total_files,
            "total_chunks": total_chunks,
            "total_symbols": total_symbols,
            "languages": languages,
        }

    def is_initialized(self) -> bool:
        """Return ``True`` if the database exists and contains indexed files.

        Returns
        -------
        bool
        """
        if not self.db_path.exists():
            return False
        try:
            conn = self._get_conn()
            count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            return count > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
