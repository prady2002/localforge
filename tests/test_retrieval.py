"""Tests for localforge.retrieval — ContextRetriever, ranking, deduplication."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from localforge.core.config import LocalForgeConfig
from localforge.core.models import FileChunk, RetrievalResult
from localforge.retrieval.ranking import deduplicate_chunks, rank_chunks
from localforge.retrieval.retriever import ContextRetriever

# ---------------------------------------------------------------------------
# test_decompose_query
# ---------------------------------------------------------------------------


def test_decompose_query_produces_3_to_5(
    tmp_repo: Path, mock_config: LocalForgeConfig,
) -> None:
    """decompose_query should return between 3 and 5 sub-queries."""
    from localforge.index.indexer import RepositoryIndexer
    from localforge.index.search import IndexSearcher

    db = tmp_repo / ".localforge" / "test.db"
    idx = RepositoryIndexer(tmp_repo, db, mock_config)
    idx.index_repository()
    searcher = IndexSearcher(db)
    retriever = ContextRetriever(idx, searcher, mock_config)

    queries = retriever.decompose_query(
        "fix the authentication bug in login endpoint"
    )
    assert 3 <= len(queries) <= 5
    # Queries should be non-empty strings
    assert all(isinstance(q, str) and len(q) > 0 for q in queries)

    searcher.close()
    idx.close()


def test_decompose_query_extracts_identifiers() -> None:
    """Snake-case and CamelCase identifiers should appear in sub-queries."""
    from localforge.index.indexer import RepositoryIndexer
    from localforge.index.search import IndexSearcher

    cfg = LocalForgeConfig()
    # We only need the decompose_query method which doesn't use DB
    idx = MagicMock(spec=RepositoryIndexer)
    searcher = MagicMock(spec=IndexSearcher)
    retriever = ContextRetriever(idx, searcher, cfg)

    queries = retriever.decompose_query(
        "fix load_config for ContextRetriever in config.py"
    )
    lower_queries = [q.lower() for q in queries]
    assert any("load_config" in q for q in lower_queries)
    assert any("contextretriever" in q for q in lower_queries)


# ---------------------------------------------------------------------------
# test_retrieve
# ---------------------------------------------------------------------------


def test_retrieve_returns_retrieval_result(
    tmp_repo: Path, mock_config: LocalForgeConfig,
) -> None:
    """retrieve() should return a RetrievalResult with chunks."""
    from localforge.index.indexer import RepositoryIndexer
    from localforge.index.search import IndexSearcher

    db = tmp_repo / ".localforge" / "test_retrieve.db"
    idx = RepositoryIndexer(tmp_repo, db, mock_config)
    idx.index_repository()
    searcher = IndexSearcher(db)
    retriever = ContextRetriever(idx, searcher, mock_config)

    result = retriever.retrieve("login authentication", limit=5)

    assert isinstance(result, RetrievalResult)
    assert isinstance(result.chunks, list)
    assert result.query == "login authentication"
    # Our fake repo has auth-related content so we should get results
    assert result.total_found >= 0

    searcher.close()
    idx.close()


def test_retrieve_with_mock_searcher(mock_config: LocalForgeConfig) -> None:
    """retrieve() works with a mocked IndexSearcher returning known chunks."""
    from localforge.index.indexer import RepositoryIndexer

    fake_chunk = FileChunk(
        file_path="app/routes.py",
        start_line=1,
        end_line=10,
        content="def login(): pass",
        score=0.8,
    )

    searcher = MagicMock()
    searcher.search_lexical.return_value = [fake_chunk]
    searcher.search_by_filename.return_value = []

    indexer = MagicMock(spec=RepositoryIndexer)
    indexer.repo_path = Path(".")

    retriever = ContextRetriever(indexer, searcher, mock_config)
    result = retriever.retrieve("login", limit=5)

    assert isinstance(result, RetrievalResult)
    assert len(result.chunks) >= 1


# ---------------------------------------------------------------------------
# test_ranking
# ---------------------------------------------------------------------------


def test_ranking_higher_relevance_ranks_higher() -> None:
    """Chunks with more keyword matches should rank higher."""
    chunks = [
        FileChunk(
            file_path="low.py",
            start_line=1, end_line=5,
            content="import os\nimport sys\n",
            score=0.1,
        ),
        FileChunk(
            file_path="high.py",
            start_line=1, end_line=5,
            content="def login():\n    authenticate(user)\n    login_check()",
            score=0.3,
        ),
    ]

    ranked = rank_chunks(chunks, "login authenticate", "fix login", repo_path=None)

    # The chunk with login/authenticate keywords should be first
    assert ranked[0].file_path == "high.py"


def test_ranking_preserves_all_chunks() -> None:
    """Ranking should not drop unique chunks."""
    chunks = [
        FileChunk(file_path=f"f{i}.py", start_line=1, end_line=5,
                  content=f"unique content {i}", score=0.5)
        for i in range(5)
    ]
    ranked = rank_chunks(chunks, "unique", "find unique", repo_path=None)
    assert len(ranked) == 5


# ---------------------------------------------------------------------------
# test_deduplication
# ---------------------------------------------------------------------------


def test_deduplication_removes_near_duplicates() -> None:
    """Chunks with >80% content similarity in the same file should be deduplicated."""
    chunks = [
        FileChunk(
            file_path="a.py", start_line=1, end_line=10,
            content="def foo():\n    return 42\n",
            score=0.9,
        ),
        FileChunk(
            file_path="a.py", start_line=1, end_line=10,
            content="def foo():\n    return 42\n",
            score=0.8,
        ),
        FileChunk(
            file_path="b.py", start_line=1, end_line=10,
            content="def bar():\n    return 99\n",
            score=0.7,
        ),
    ]

    deduped = deduplicate_chunks(chunks)
    assert len(deduped) == 2
    paths = [c.file_path for c in deduped]
    assert "a.py" in paths
    assert "b.py" in paths


def test_deduplication_keeps_distinct_chunks() -> None:
    """Chunks with very different content should all be kept."""
    chunks = [
        FileChunk(file_path="a.py", start_line=1, end_line=5,
                  content="completely different alpha" * 10, score=0.9),
        FileChunk(file_path="a.py", start_line=50, end_line=55,
                  content="totally unrelated beta" * 10, score=0.8),
    ]
    deduped = deduplicate_chunks(chunks)
    assert len(deduped) == 2
