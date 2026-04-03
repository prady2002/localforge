"""Chunk ranking and deduplication for the retrieval subsystem."""

from __future__ import annotations

import difflib
import re
import time
from pathlib import Path

from localforge.core.models import FileChunk


def rank_chunks(
    chunks: list[FileChunk],
    query: str,
    task: str,
    repo_path: Path | None = None,
) -> list[FileChunk]:
    """Score and sort *chunks* by relevance to the *query* and *task*.

    Scoring is a weighted combination of:
    * **Lexical match** – the ``score`` already set on each chunk by the
      search layer.
    * **Term-frequency** – how often query keywords appear in the chunk
      content.
    * **Path relevance** – bonus when the file path contains keywords from
      the task.
    * **Recency** – small boost for files modified recently on disk.
    * **Deduplication penalty** – chunks with very similar content receive
      a score reduction so that variety is preserved.

    Parameters
    ----------
    chunks:
        Candidate chunks to rank.
    query:
        The specific sub-query that produced these chunks.
    task:
        The original user task string.
    repo_path:
        Repository root (used for recency checks).  When *None* recency
        boosting is skipped.

    Returns
    -------
    list[FileChunk]
        De-duplicated chunks sorted by combined score (descending).
    """
    if not chunks:
        return []

    keywords = _extract_keywords(query + " " + task)

    scored: list[tuple[float, FileChunk]] = []
    seen_contents: list[str] = []

    for chunk in chunks:
        score = chunk.score

        # --- TF score ------------------------------------------------
        content_lower = chunk.content.lower()
        tf = sum(content_lower.count(kw) for kw in keywords)
        score += min(tf * 0.05, 0.5)  # cap contribution

        # --- Path relevance ------------------------------------------
        path_lower = chunk.file_path.lower()
        if any(kw in path_lower for kw in keywords):
            score += 0.2

        # --- Recency boost -------------------------------------------
        if repo_path is not None:
            score += _recency_boost(repo_path / chunk.file_path)

        # --- Deduplication penalty -----------------------------------
        for prev in seen_contents:
            ratio = difflib.SequenceMatcher(None, chunk.content, prev).quick_ratio()
            if ratio > 0.80:
                score -= 0.5
                break

        seen_contents.append(chunk.content)
        scored.append((score, chunk))

    scored.sort(key=lambda t: t[0], reverse=True)

    ranked = [chunk.model_copy(update={"score": round(s, 4)}) for s, chunk in scored]
    return deduplicate_chunks(ranked)


def deduplicate_chunks(chunks: list[FileChunk]) -> list[FileChunk]:
    """Remove chunks whose content overlaps >80 % with an earlier chunk.

    Uses :class:`difflib.SequenceMatcher` for pairwise comparison.

    Parameters
    ----------
    chunks:
        Ordered list of chunks (assumed already sorted by score).

    Returns
    -------
    list[FileChunk]
        Filtered list preserving the original order.
    """
    kept: list[FileChunk] = []
    for chunk in chunks:
        is_dup = False
        for existing in kept:
            if chunk.file_path == existing.file_path:
                ratio = difflib.SequenceMatcher(
                    None, chunk.content, existing.content
                ).ratio()
                if ratio > 0.80:
                    is_dup = True
                    break
        if not is_dup:
            kept.append(chunk)
    return kept


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _extract_keywords(text: str) -> list[str]:
    """Pull meaningful lowercase keywords from *text*.

    Splits on whitespace and punctuation, removes short / stop words.
    """
    _STOP_WORDS = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "in", "on", "at", "to", "for", "of", "and", "or", "not",
        "it", "this", "that", "with", "from", "by", "as", "if",
        "but", "do", "does", "did", "will", "shall", "should",
        "can", "could", "may", "might", "i", "we", "you", "he",
        "she", "they", "my", "our", "your", "its", "fix", "add",
        "make", "update", "use",
    }
    tokens = re.split(r"[^a-zA-Z0-9_]+", text.lower())
    return [t for t in tokens if t and len(t) > 1 and t not in _STOP_WORDS]


def _recency_boost(file_path: Path) -> float:
    """Return a small score bonus if *file_path* was modified recently.

    Returns ``0.1`` when the file was modified within the last 24 hours,
    ``0.05`` within 7 days, else ``0.0``.
    """
    try:
        mtime = file_path.stat().st_mtime
    except OSError:
        return 0.0

    age = time.time() - mtime
    if age < 86_400:       # 24 h
        return 0.10
    if age < 604_800:      # 7 d
        return 0.05
    return 0.0
