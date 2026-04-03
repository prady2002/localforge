"""Search interface over the localforge index database.

Provides lexical (FTS5), filename, and symbol search as well as chunk
retrieval helpers.
"""

from __future__ import annotations

import difflib
import logging
import sqlite3
from pathlib import Path
from typing import Any

from localforge.core.models import FileChunk

logger = logging.getLogger(__name__)


class IndexSearcher:
    """Query the SQLite index built by :class:`RepositoryIndexer`."""

    def __init__(self, db_path: Path) -> None:
        """Open a read-only connection to the index database.

        Parameters
        ----------
        db_path:
            Path to the SQLite database file created by the indexer.
        """
        self.db_path = db_path.resolve()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Return the current connection, creating one if needed."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    # ------------------------------------------------------------------
    # Full-text search
    # ------------------------------------------------------------------

    def search_lexical(self, query: str, limit: int = 20) -> list[FileChunk]:
        """Run a full-text search over indexed chunks using SQLite FTS5.

        Parameters
        ----------
        query:
            Free-text search query.  FTS5 query syntax is supported
            (e.g. ``"foo AND bar"``, ``"foo OR bar"``).
        limit:
            Maximum number of results to return.

        Returns
        -------
        list[FileChunk]
            Matching chunks ordered by relevance (descending rank).
        """
        conn = self._get_conn()
        results: list[FileChunk] = []

        # Sanitise the query: strip leading/trailing whitespace.
        query = query.strip()
        if not query:
            return results

        # Escape FTS5 special characters so arbitrary user input is safe.
        # FTS5 treats characters like ., -, *, ^, etc. as syntax operators.
        # Wrapping each token in double quotes makes them literal.
        fts_query = " ".join(f'"{token}"' for token in query.split() if token)

        try:
            rows = conn.execute(
                """
                SELECT c.id, c.file_id, c.start_line, c.end_line, c.content,
                       f.relative_path,
                       rank
                  FROM chunks_fts
                  JOIN chunks c ON c.id = chunks_fts.rowid
                  JOIN files  f ON f.id = c.file_id
                 WHERE chunks_fts MATCH ?
                 ORDER BY rank
                 LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()

            for row in rows:
                results.append(
                    FileChunk(
                        file_path=row["relative_path"],
                        start_line=row["start_line"],
                        end_line=row["end_line"],
                        content=row["content"],
                        score=-row["rank"],  # FTS5 rank is negative; invert for intuitive ordering
                    )
                )
        except Exception:
            logger.exception("Lexical search failed for query: %s", query)

        return results

    # ------------------------------------------------------------------
    # Filename search
    # ------------------------------------------------------------------

    def search_by_filename(self, pattern: str, limit: int = 10) -> list[FileChunk]:
        """Search for files whose path fuzzy-matches *pattern*.

        Uses :class:`difflib.SequenceMatcher` to score each indexed file
        path against the pattern and returns the top matches.  For every
        matching file the first chunk is returned so the caller has some
        content to display.

        Parameters
        ----------
        pattern:
            Substring or approximate file name to search for.
        limit:
            Maximum number of results to return.

        Returns
        -------
        list[FileChunk]
            One chunk per matching file, ordered by similarity score
            (descending).
        """
        conn = self._get_conn()
        results: list[FileChunk] = []

        pattern = pattern.strip()
        if not pattern:
            return results

        try:
            files = conn.execute("SELECT id, relative_path FROM files").fetchall()
        except Exception:
            logger.exception("Filename search failed for pattern: %s", pattern)
            return results

        scored: list[tuple[float, Any]] = []
        for f in files:
            ratio = difflib.SequenceMatcher(None, pattern.lower(), f["relative_path"].lower()).ratio()
            scored.append((ratio, f))

        scored.sort(key=lambda t: t[0], reverse=True)

        for score, f in scored[:limit]:
            if score < 0.1:
                continue
            try:
                chunk_row = conn.execute(
                    """
                    SELECT start_line, end_line, content
                      FROM chunks
                     WHERE file_id = ?
                     ORDER BY start_line
                     LIMIT 1
                    """,
                    (f["id"],),
                ).fetchone()

                if chunk_row:
                    results.append(
                        FileChunk(
                            file_path=f["relative_path"],
                            start_line=chunk_row["start_line"],
                            end_line=chunk_row["end_line"],
                            content=chunk_row["content"],
                            score=round(score, 4),
                        )
                    )
            except Exception:
                logger.exception("Error fetching chunk for file %s", f["relative_path"])

        return results

    # ------------------------------------------------------------------
    # Symbol search
    # ------------------------------------------------------------------

    def search_symbols(self, name: str, kind: str | None = None) -> list[dict[str, Any]]:
        """Search the symbols table for definitions matching *name*.

        Parameters
        ----------
        name:
            Symbol name (or prefix) to search for.  The search is
            case-insensitive and uses a ``LIKE`` pattern.
        kind:
            Optional filter on symbol kind (e.g. ``"function"``,
            ``"class"``).

        Returns
        -------
        list[dict]
            Each dict has keys ``file_path``, ``line``, ``kind``, ``name``.
        """
        conn = self._get_conn()
        results: list[dict[str, Any]] = []

        name = name.strip()
        if not name:
            return results

        try:
            if kind:
                rows = conn.execute(
                    """
                    SELECT s.name, s.kind, s.line, f.relative_path
                      FROM symbols s
                      JOIN files f ON f.id = s.file_id
                     WHERE s.name LIKE ? AND s.kind = ?
                     ORDER BY s.name
                    """,
                    (f"%{name}%", kind),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT s.name, s.kind, s.line, f.relative_path
                      FROM symbols s
                      JOIN files f ON f.id = s.file_id
                     WHERE s.name LIKE ?
                     ORDER BY s.name
                    """,
                    (f"%{name}%",),
                ).fetchall()

            for row in rows:
                results.append(
                    {
                        "file_path": row["relative_path"],
                        "line": row["line"],
                        "kind": row["kind"],
                        "name": row["name"],
                    }
                )
        except Exception:
            logger.exception("Symbol search failed for name: %s", name)

        return results

    # ------------------------------------------------------------------
    # Chunk retrieval
    # ------------------------------------------------------------------

    def get_file_chunks(self, file_path: str) -> list[FileChunk]:
        """Return all chunks for a specific file.

        Parameters
        ----------
        file_path:
            Repo-relative path (forward slashes).

        Returns
        -------
        list[FileChunk]
            Chunks in line order.
        """
        conn = self._get_conn()
        results: list[FileChunk] = []

        try:
            rows = conn.execute(
                """
                SELECT c.start_line, c.end_line, c.content
                  FROM chunks c
                  JOIN files f ON f.id = c.file_id
                 WHERE f.relative_path = ?
                 ORDER BY c.start_line
                """,
                (file_path,),
            ).fetchall()

            for row in rows:
                results.append(
                    FileChunk(
                        file_path=file_path,
                        start_line=row["start_line"],
                        end_line=row["end_line"],
                        content=row["content"],
                        score=0.0,
                    )
                )
        except Exception:
            logger.exception("Failed to get chunks for file: %s", file_path)

        return results

    def get_chunk_context(self, chunk: FileChunk, surrounding_lines: int = 5) -> FileChunk:
        """Expand *chunk* by fetching surrounding lines from the database.

        Looks up the same file's chunks in the database and merges
        overlapping content to provide additional context around the
        original chunk boundaries.

        Parameters
        ----------
        chunk:
            The chunk to expand.
        surrounding_lines:
            Number of extra lines to include above and below.

        Returns
        -------
        FileChunk
            A new chunk with expanded boundaries and merged content.
        """
        conn = self._get_conn()
        desired_start = max(1, chunk.start_line - surrounding_lines)
        desired_end = chunk.end_line + surrounding_lines

        try:
            rows = conn.execute(
                """
                SELECT c.start_line, c.end_line, c.content
                  FROM chunks c
                  JOIN files f ON f.id = c.file_id
                 WHERE f.relative_path = ?
                   AND c.end_line >= ? AND c.start_line <= ?
                 ORDER BY c.start_line
                """,
                (chunk.file_path, desired_start, desired_end),
            ).fetchall()

            if not rows:
                return chunk

            # Merge overlapping chunk content line by line.
            merged_lines: dict[int, str] = {}
            for row in rows:
                content_lines = row["content"].splitlines(keepends=True)
                for i, line in enumerate(content_lines):
                    lineno = row["start_line"] + i
                    if lineno not in merged_lines:
                        merged_lines[lineno] = line

            # Trim to the desired range.
            final_start = max(desired_start, min(merged_lines.keys()))
            final_end = min(desired_end, max(merged_lines.keys()))

            merged_content = "".join(
                merged_lines.get(i, "\n") for i in range(final_start, final_end + 1)
            )

            return FileChunk(
                file_path=chunk.file_path,
                start_line=final_start,
                end_line=final_end,
                content=merged_content,
                score=chunk.score,
            )
        except Exception:
            logger.exception("Failed to expand chunk context for %s", chunk.file_path)
            return chunk

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
