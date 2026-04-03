"""Context retrieval engine for localforge.

Orchestrates multiple search strategies (FTS, filename, ripgrep) to find
the most relevant code chunks for a given natural-language task.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from localforge.core.config import LocalForgeConfig, get_model_profile_settings
from localforge.core.models import FileChunk, PlanStep, RetrievalResult
from localforge.index.indexer import RepositoryIndexer
from localforge.index.search import IndexSearcher
from localforge.retrieval.ranking import deduplicate_chunks, rank_chunks

logger = logging.getLogger(__name__)


class ContextRetriever:
    """Find relevant code chunks for a task by combining multiple search strategies."""

    def __init__(
        self,
        indexer: RepositoryIndexer,
        searcher: IndexSearcher,
        config: LocalForgeConfig,
    ) -> None:
        """Initialise the retriever.

        Parameters
        ----------
        indexer:
            :class:`RepositoryIndexer` used for repository metadata and path
            resolution.
        searcher:
            :class:`IndexSearcher` used for FTS and filename queries.
        config:
            Application configuration.
        """
        self.indexer = indexer
        self.searcher = searcher
        self.config = config

    # ------------------------------------------------------------------
    # Query decomposition
    # ------------------------------------------------------------------

    def decompose_query(self, task: str) -> list[str]:
        """Split a natural-language *task* into 3–5 focused search queries.

        Uses pure string heuristics — no LLM call:
        * Extract snake_case and CamelCase identifiers.
        * Extract quoted strings.
        * Pull significant nouns / keywords.
        * Detect file-name hints (tokens ending in common extensions).

        Parameters
        ----------
        task:
            The user's free-form task description.

        Returns
        -------
        list[str]
            Between 3 and 5 de-duplicated search queries.
        """
        queries: list[str] = []

        # 1. Quoted strings
        quoted = re.findall(r'["\']([^"\']+)["\']', task)
        queries.extend(quoted)

        # 2. snake_case identifiers (e.g. load_config, get_user)
        snake = re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", task)
        queries.extend(snake)

        # 3. CamelCase identifiers (e.g. ContextRetriever, FileChunk)
        camel = re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b", task)
        queries.extend(camel)

        # 4. File-name hints (tokens that look like filenames)
        file_pattern = re.findall(
            r"\b[\w/\\.-]+\.(?:py|js|ts|go|rs|java|cpp|c|rb|yaml|yml|json|toml|md)\b",
            task,
        )
        queries.extend(file_pattern)

        # 5. Significant keywords — nouns / technical terms > 3 chars
        _STOP = {
            "the", "and", "for", "that", "this", "with", "from", "have",
            "has", "not", "are", "was", "were", "been", "being", "will",
            "should", "could", "would", "can", "does", "did", "but",
            "about", "into", "when", "where", "which", "while", "what",
            "there", "their", "also", "just", "some", "need", "make",
            "like", "than", "then", "these", "those", "each", "every",
            "such", "only",
        }
        words = re.findall(r"\b[a-zA-Z]{4,}\b", task)
        keywords = [w.lower() for w in words if w.lower() not in _STOP]
        queries.extend(keywords)

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for q in queries:
            normalised = q.strip().lower()
            if normalised and normalised not in seen:
                seen.add(normalised)
                unique.append(q.strip())

        # Clamp to 3–5 queries
        if len(unique) < 3:
            # Pad with the first few words of the original task
            fallback_tokens = task.split()[:5]
            for t in fallback_tokens:
                t_lower = t.strip().lower()
                if len(t_lower) > 2 and t_lower not in seen:
                    seen.add(t_lower)
                    unique.append(t.strip())
                if len(unique) >= 3:
                    break

        return unique[:5]

    # ------------------------------------------------------------------
    # Main retrieval
    # ------------------------------------------------------------------

    def retrieve(self, task: str, limit: int = 15) -> RetrievalResult:
        """Retrieve relevant code chunks for *task*.

        Pipeline:
        1. Decompose the task into sub-queries via :meth:`decompose_query`.
        2. For each sub-query, run lexical search and filename search.
        3. Optionally run ``ripgrep`` if available.
        4. Merge, deduplicate, rank, and return the top *limit* chunks.

        Parameters
        ----------
        task:
            Natural-language task description.
        limit:
            Maximum number of chunks to return.

        Returns
        -------
        RetrievalResult
        """
        queries = self.decompose_query(task)
        all_chunks: list[FileChunk] = []

        profile = get_model_profile_settings(self.config.model_profile)
        per_query_limit = max(limit, profile.retrieval_limit)

        for q in queries:
            try:
                lexical = self.searcher.search_lexical(q, limit=per_query_limit)
                all_chunks.extend(lexical)
            except Exception:
                logger.debug("Lexical search failed for sub-query: %s", q, exc_info=True)

            try:
                filename = self.searcher.search_by_filename(q, limit=5)
                all_chunks.extend(filename)
            except Exception:
                logger.debug("Filename search failed for sub-query: %s", q, exc_info=True)

        # Ripgrep pass
        repo_path = self.indexer.repo_path
        for q in queries:
            try:
                rg_results = self.ripgrep_search(q, repo_path, limit=10)
                all_chunks.extend(rg_results)
            except Exception:
                logger.debug("Ripgrep search failed for sub-query: %s", q, exc_info=True)

        # Boost files explicitly mentioned in the task
        _boost_mentioned_files(all_chunks, task)

        # Rank and deduplicate
        ranked = rank_chunks(all_chunks, " ".join(queries), task, repo_path=repo_path)

        return RetrievalResult(
            chunks=ranked[:limit],
            query=task,
            total_found=len(ranked),
        )

    # ------------------------------------------------------------------
    # Patch-targeted retrieval
    # ------------------------------------------------------------------

    def retrieve_for_patch(
        self,
        plan_step: PlanStep,
        existing_chunks: list[FileChunk],
    ) -> list[FileChunk]:
        """Fetch additional context for files in *plan_step* that are not yet covered.

        Parameters
        ----------
        plan_step:
            A single step from the agent plan.
        existing_chunks:
            Chunks that have already been retrieved in previous steps.

        Returns
        -------
        list[FileChunk]
            New chunks for uncovered files.
        """
        existing_files = {c.file_path for c in existing_chunks}
        new_chunks: list[FileChunk] = []

        for file_path in plan_step.files_involved:
            if file_path in existing_files:
                continue
            try:
                file_chunks = self.searcher.get_file_chunks(file_path)
                new_chunks.extend(file_chunks)
            except Exception:
                logger.debug("Failed to retrieve chunks for %s", file_path, exc_info=True)

        # Also do a targeted lexical search on the step description
        try:
            lexical = self.searcher.search_lexical(plan_step.description, limit=5)
            new_chunks.extend(lexical)
        except Exception:
            logger.debug(
                "Lexical search failed for plan step: %s",
                plan_step.description,
                exc_info=True,
            )

        return deduplicate_chunks(new_chunks)

    # ------------------------------------------------------------------
    # Ripgrep integration
    # ------------------------------------------------------------------

    def ripgrep_search(
        self,
        pattern: str,
        repo_path: Path,
        limit: int = 10,
    ) -> list[FileChunk]:
        """Search the repository with ``ripgrep`` (``rg``) if available.

        Runs::

            rg --json -n --max-count 3 -i <pattern> <repo_path>

        and parses the JSON-lines output into :class:`FileChunk` objects.

        Parameters
        ----------
        pattern:
            Regex or literal pattern to search for.
        repo_path:
            Root directory to search.
        limit:
            Maximum number of chunks to return.

        Returns
        -------
        list[FileChunk]
            Matching chunks, or an empty list when ``rg`` is not installed.
        """
        if shutil.which("rg") is None:
            return []

        try:
            result = subprocess.run(
                [
                    "rg", "--json", "-n", "--max-count", "3",
                    "-i", "--", pattern, str(repo_path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("ripgrep invocation failed: %s", exc)
            return []

        chunks: list[FileChunk] = []
        for line in result.stdout.splitlines():
            if len(chunks) >= limit:
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if obj.get("type") != "match":
                continue

            data = obj.get("data", {})
            path_obj = data.get("path", {})
            abs_path = path_obj.get("text", "")
            line_number = data.get("line_number", 1)
            lines_obj = data.get("lines", {})
            content = lines_obj.get("text", "")

            if not abs_path or not content:
                continue

            # Convert to repo-relative path
            try:
                rel = str(Path(abs_path).resolve().relative_to(repo_path.resolve()))
                rel = rel.replace("\\", "/")
            except (ValueError, OSError):
                continue

            chunks.append(
                FileChunk(
                    file_path=rel,
                    start_line=line_number,
                    end_line=line_number,
                    content=content,
                    score=0.3,  # baseline score for rg matches
                )
            )

        return chunks


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _boost_mentioned_files(chunks: list[FileChunk], task: str) -> None:
    """In-place score boost for chunks whose file path is mentioned in *task*."""
    task_lower = task.lower()
    for chunk in chunks:
        # Check both the full relative path and just the filename
        fname = Path(chunk.file_path).name.lower()
        if fname in task_lower or chunk.file_path.lower() in task_lower:
            chunk.score += 0.3
