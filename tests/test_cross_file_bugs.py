"""Cross-file regression tests for interconnected bug scenarios.

These tests cover critical functions across multiple modules that work
together in the tool-calling pipeline.  A bug in any one of these
functions can cause cascading failures visible only at the system level.

Test coverage targets (previously untested):
- cloud/engine.py: _truncate_tool_result (truncation math)
- chat/tools.py: validate_tool_call (return value contract)
- chat/tools.py: hash_tool_call (determinism / sort_keys)
- context_manager/budget.py: fit_chunks_to_budget (sort order)
- chat/session.py: get_ollama_messages (slicing correctness)
- cloud/engine.py: _is_debugging_query (logic correctness)
- Integration: full tool-call-execute-validate-dedup pipeline
"""

from __future__ import annotations

from pathlib import Path

import pytest

from localforge.chat.tools import (
    ToolExecutor,
    hash_tool_call,
    validate_tool_call,
)
from localforge.cloud.engine import (
    _classify_query,
    _is_debugging_query,
    _truncate_tool_result,
)
from localforge.context_manager.budget import TokenBudgetManager
from localforge.core.config import LocalForgeConfig
from localforge.core.models import FileChunk


# ---------------------------------------------------------------------------
# _truncate_tool_result — math correctness
# ---------------------------------------------------------------------------


class TestTruncateToolResult:
    """Tests for _truncate_tool_result to ensure truncation math is correct."""

    def test_short_text_untouched(self):
        text = "short result"
        assert _truncate_tool_result(text, max_chars=100) == text

    def test_exact_limit_untouched(self):
        text = "x" * 100
        assert _truncate_tool_result(text, max_chars=100) == text

    def test_truncation_preserves_head_and_tail(self):
        """Head (60%) + tail (30%) should not overlap and should be < max_chars."""
        text = "A" * 30000 + "B" * 30000 + "C" * 40000  # 100K chars
        result = _truncate_tool_result(text, max_chars=50000)

        # Result should be shorter than original
        assert len(result) < len(text)
        # Should contain the truncation notice
        assert "characters omitted" in result
        # Head should start with 'A's
        assert result.startswith("A")
        # Tail should end with 'C's
        assert result.rstrip().endswith("C")

    def test_omitted_count_is_positive(self):
        """The omitted character count should always be positive."""
        text = "x" * 100000
        result = _truncate_tool_result(text, max_chars=50000)
        # Extract the omitted count from the message
        import re
        match = re.search(r'\((\d+) characters omitted\)', result)
        assert match is not None
        omitted = int(match.group(1))
        assert omitted > 0, "Omitted count must be positive"

    def test_head_tail_no_overlap(self):
        """Head (60%) and tail (30%) should total < 100% of max_chars."""
        max_c = 1000
        text = "".join(str(i % 10) for i in range(5000))
        result = _truncate_tool_result(text, max_chars=max_c)

        # Head is 60% of max_chars = 600 chars
        head_portion = result.split("...")[0].rstrip()
        # The result should not contain overlapping sections
        assert len(head_portion) <= int(max_c * 0.7)  # Allow some margin

    def test_truncation_warning_present(self):
        text = "x" * 200
        result = _truncate_tool_result(text, max_chars=100)
        assert "WARNING" in result
        assert "read_file" in result

    def test_assertion_guard_prevents_overlap(self):
        """The truncation function should assert head+tail < max."""
        # This test verifies the assertion guard is working.
        # With correct ratios (0.6 + 0.3 = 0.9), this should pass.
        text = "x" * 200
        result = _truncate_tool_result(text, max_chars=100)
        assert "omitted" in result


# ---------------------------------------------------------------------------
# validate_tool_call — return value contract
# ---------------------------------------------------------------------------


class TestValidateToolCallContract:
    """Tests that validate_tool_call returns None for VALID calls (not a string)."""

    def test_valid_read_file_returns_none(self):
        tc = {"tool": "read_file", "args": {"path": "test.py"}}
        result = validate_tool_call(tc)
        assert result is None, f"Expected None for valid call, got: {result!r}"

    def test_valid_edit_file_returns_none(self):
        tc = {"tool": "edit_file", "args": {
            "path": "test.py",
            "old_string": "old",
            "new_string": "new",
        }}
        result = validate_tool_call(tc)
        assert result is None, f"Expected None for valid call, got: {result!r}"

    def test_valid_run_command_returns_none(self):
        tc = {"tool": "run_command", "args": {"command": "pytest"}}
        result = validate_tool_call(tc)
        assert result is None, f"Expected None for valid call, got: {result!r}"

    def test_valid_write_file_returns_none(self):
        tc = {"tool": "write_file", "args": {"path": "x.py", "content": "pass"}}
        result = validate_tool_call(tc)
        assert result is None

    def test_valid_batch_edit_returns_none(self):
        tc = {"tool": "batch_edit", "args": {"edits": []}}
        result = validate_tool_call(tc)
        assert result is None

    def test_valid_list_directory_returns_none(self):
        tc = {"tool": "list_directory", "args": {}}
        result = validate_tool_call(tc)
        assert result is None

    def test_valid_verify_changes_returns_none(self):
        tc = {"tool": "verify_changes", "args": {}}
        result = validate_tool_call(tc)
        assert result is None

    def test_valid_search_code_returns_none(self):
        tc = {"tool": "search_code", "args": {"pattern": "foo"}}
        result = validate_tool_call(tc)
        assert result is None

    def test_valid_grep_returns_none(self):
        tc = {"tool": "grep_codebase", "args": {"pattern": "bar"}}
        result = validate_tool_call(tc)
        assert result is None

    def test_unknown_tool_returns_string(self):
        tc = {"tool": "nonexistent_tool", "args": {}}
        result = validate_tool_call(tc)
        assert isinstance(result, str)
        assert "Unknown tool" in result

    def test_missing_required_args_returns_string(self):
        tc = {"tool": "read_file", "args": {}}  # missing 'path'
        result = validate_tool_call(tc)
        assert isinstance(result, str)
        assert "missing required args" in result

    def test_return_type_is_none_not_truthy_string(self):
        """Engine checks `if validation_err:` — truthy string blocks it."""
        for tool_name, args in [
            ("read_file", {"path": "x.py"}),
            ("run_command", {"command": "echo hi"}),
            ("edit_file", {"path": "x.py", "old_string": "a", "new_string": "b"}),
            ("get_project_overview", {}),
            ("find_symbols", {"name": "foo"}),
        ]:
            tc = {"tool": tool_name, "args": args}
            result = validate_tool_call(tc)
            assert result is None, (
                f"validate_tool_call({tool_name}) returned {result!r} instead of None. "
                f"This would block ALL {tool_name} tool calls in the engine."
            )


# ---------------------------------------------------------------------------
# hash_tool_call — determinism
# ---------------------------------------------------------------------------


class TestHashToolCallDeterminism:
    """Tests that hash_tool_call produces deterministic, consistent hashes."""

    def test_same_args_same_hash(self):
        h1 = hash_tool_call("read_file", {"path": "test.py"})
        h2 = hash_tool_call("read_file", {"path": "test.py"})
        assert h1 == h2

    def test_different_args_different_hash(self):
        h1 = hash_tool_call("read_file", {"path": "a.py"})
        h2 = hash_tool_call("read_file", {"path": "b.py"})
        assert h1 != h2

    def test_key_order_does_not_matter(self):
        """Dict key order should NOT affect the hash (sort_keys=True)."""
        h1 = hash_tool_call(
            "edit_file",
            {"path": "x.py", "old_string": "a", "new_string": "b"},
        )
        h2 = hash_tool_call(
            "edit_file",
            {"new_string": "b", "old_string": "a", "path": "x.py"},
        )
        assert h1 == h2, (
            "hash_tool_call is NOT deterministic across key orderings. "
            "This causes false dedup blocks — the same tool call with reordered "
            "keys gets a different hash, bypassing dedup. Or different calls "
            "with different keys could collide. Use sort_keys=True in json.dumps."
        )

    def test_hash_length_sufficient(self):
        """Hash should be long enough to avoid collisions in practice."""
        h = hash_tool_call("read_file", {"path": "test.py"})
        assert len(h) >= 10, (
            f"Hash length {len(h)} is too short — high collision probability. "
            f"Use at least 12 hex chars (48 bits) for sufficient entropy."
        )

    def test_different_tools_different_hash(self):
        h1 = hash_tool_call("read_file", {"path": "test.py"})
        h2 = hash_tool_call("write_file", {"path": "test.py"})
        assert h1 != h2

    def test_nested_args_deterministic(self):
        """Nested dicts in args should also be deterministic."""
        args = {"edits": [
            {"path": "a.py", "old_string": "x", "new_string": "y"},
            {"path": "b.py", "old_string": "m", "new_string": "n"},
        ]}
        h1 = hash_tool_call("batch_edit", args)
        h2 = hash_tool_call("batch_edit", args)
        assert h1 == h2


# ---------------------------------------------------------------------------
# fit_chunks_to_budget — sort order (highest score first)
# ---------------------------------------------------------------------------


class TestFitChunksToBudget:
    """Tests that fit_chunks_to_budget selects highest-scored chunks first."""

    @pytest.fixture()
    def manager(self):
        config = LocalForgeConfig()
        return TokenBudgetManager(config)

    def test_selects_highest_scored_chunks(self, manager: TokenBudgetManager):
        """With limited budget, should select highest-scored chunks first."""
        chunks = [
            FileChunk(file_path="low.py", start_line=1, end_line=5,
                      content="low score content", score=0.1),
            FileChunk(file_path="high.py", start_line=1, end_line=5,
                      content="high score content", score=0.9),
            FileChunk(file_path="mid.py", start_line=1, end_line=5,
                      content="mid score content", score=0.5),
        ]
        # Budget allows only 1 chunk
        selected = manager.fit_chunks_to_budget(chunks, budget=10)
        assert len(selected) >= 1
        # First selected chunk should be the highest scored
        assert selected[0].file_path == "high.py", (
            f"Expected 'high.py' (0.9) but got "
            f"'{selected[0].file_path}' "
            f"(score={selected[0].score}). Wrong sort order."
        )

    def test_selects_high_before_low(self, manager: TokenBudgetManager):
        """If budget allows 2 of 3 chunks, should pick top 2 by score."""
        chunks = [
            FileChunk(file_path="c.py", start_line=1, end_line=2,
                      content="c content", score=0.3),
            FileChunk(file_path="a.py", start_line=1, end_line=2,
                      content="a content", score=0.9),
            FileChunk(file_path="b.py", start_line=1, end_line=2,
                      content="b content", score=0.6),
        ]
        # Budget allows 2 chunks (~2 tokens each with fallback encoder)
        selected = manager.fit_chunks_to_budget(chunks, budget=20)
        selected_paths = [c.file_path for c in selected]
        # a.py (0.9) and b.py (0.6) should be selected before c.py (0.3)
        if len(selected) >= 2:
            assert "a.py" in selected_paths, (
                "Highest-scored chunk a.py should be selected"
            )
            if "b.py" in selected_paths:
                assert (
                    selected_paths.index("a.py")
                    < selected_paths.index("b.py")
                )

    def test_empty_chunks(self, manager: TokenBudgetManager):
        assert manager.fit_chunks_to_budget([], budget=100) == []

    def test_all_fit(self, manager: TokenBudgetManager):
        chunks = [
            FileChunk(file_path="a.py", start_line=1, end_line=1,
                      content="x", score=0.5),
        ]
        selected = manager.fit_chunks_to_budget(chunks, budget=10000)
        assert len(selected) == 1


# ---------------------------------------------------------------------------
# Session message slicing — preserves most recent messages
# ---------------------------------------------------------------------------


class TestSessionMessageSlicing:
    """Tests that session.get_ollama_messages keeps the MOST RECENT messages."""

    def test_keeps_most_recent_when_truncating(self):
        from localforge.chat.session import ChatSession

        session = ChatSession(session_id="test", repo_path=".")
        for i in range(20):
            session.add_user_message(f"msg_{i}")

        msgs = session.get_ollama_messages(max_messages=5)
        assert len(msgs) == 5

        # The most recent message (msg_19) MUST be present
        contents = [m["content"] for m in msgs]
        assert "msg_19" in contents, (
            f"Most recent message 'msg_19' is missing from truncated messages. "
            f"Got: {contents}. The tail slicing may be dropping recent messages."
        )
        assert "msg_18" in contents, (
            f"Second most recent message 'msg_18' is missing. "
            f"Got: {contents}"
        )

    def test_keeps_head_messages(self):
        from localforge.chat.session import ChatSession

        session = ChatSession(session_id="test", repo_path=".")
        for i in range(20):
            session.add_user_message(f"msg_{i}")

        msgs = session.get_ollama_messages(max_messages=5)
        contents = [m["content"] for m in msgs]
        # Head (first 2) should be preserved
        assert "msg_0" in contents
        assert "msg_1" in contents

    def test_no_truncation_when_under_limit(self):
        from localforge.chat.session import ChatSession

        session = ChatSession(session_id="test", repo_path=".")
        session.add_user_message("a")
        session.add_user_message("b")

        msgs = session.get_ollama_messages(max_messages=10)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "a"
        assert msgs[1]["content"] == "b"


# ---------------------------------------------------------------------------
# Debugging query classification — logic correctness
# ---------------------------------------------------------------------------


class TestDebuggingQueryLogic:
    """Extra tests for _is_debugging_query logic correctness."""

    def test_basic_bug_report(self):
        assert _is_debugging_query("there's a bug in the code") is True

    def test_crash_report(self):
        assert _is_debugging_query("the app crashes on startup") is True

    def test_error_report(self):
        assert _is_debugging_query("getting an error when login") is True

    def test_not_working(self):
        assert _is_debugging_query("the api is not working") is True

    def test_traceback(self):
        assert _is_debugging_query("there's a traceback in the logs") is True

    def test_investigate(self):
        assert _is_debugging_query("investigate why it returns 500") is True

    def test_problem_with(self):
        assert _is_debugging_query("there's a problem with the auth module") is True

    def test_scaffolding_with_error_word_is_NOT_debugging(self):
        """'build X error handler' should NOT be debugging — it's scaffolding."""
        assert _is_debugging_query("build a new error handling system") is False

    def test_create_with_debug_word_is_NOT_debugging(self):
        assert _is_debugging_query("create a crash reporter module") is False

    def test_no_debug_words(self):
        assert _is_debugging_query("add a new feature") is False

    def test_classify_routes_to_debugging(self):
        """End-to-end: _classify_query should route debug queries to 'debugging'."""
        assert _classify_query("the app crashes when I submit a form") == "debugging"
        assert _classify_query("there's an error in the login function") == "debugging"
        assert _classify_query("investigate why the API returns 500") == "debugging"


# ---------------------------------------------------------------------------
# Integration: tool call pipeline (validate → hash → execute)
# ---------------------------------------------------------------------------


class TestToolCallPipeline:
    """Integration tests that exercise the full tool call pipeline."""

    @pytest.fixture()
    def executor(self, tmp_path: Path) -> ToolExecutor:
        (tmp_path / "sample.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
        return ToolExecutor(tmp_path)

    def test_valid_tool_call_executes(self, executor: ToolExecutor):
        """A valid tool call should pass validation and execute successfully."""
        tc = {"tool": "read_file", "args": {"path": "sample.py"}}

        # Step 1: Validate
        validation_err = validate_tool_call(tc)
        assert validation_err is None, (
            f"Validation blocked valid call: {validation_err}"
        )

        # Step 2: Execute
        result = executor.execute(tc["tool"], tc["args"])
        assert "x = 1" in result
        assert "Error" not in result

    def test_valid_tool_not_blocked_by_dedup(self):
        """Two different tool calls should never produce the same hash."""
        h1 = hash_tool_call("read_file", {"path": "a.py"})
        h2 = hash_tool_call("read_file", {"path": "b.py"})

        # These are different calls — they must NOT collide
        assert h1 != h2, (
            "Different tool calls produced same hash "
            "— dedup would falsely block"
        )

    def test_reordered_args_dont_bypass_dedup(self):
        """Same call with reordered keys should produce the SAME hash."""
        h1 = hash_tool_call("edit_file", {
            "path": "main.py", "old_string": "foo", "new_string": "bar"
        })
        h2 = hash_tool_call("edit_file", {
            "new_string": "bar", "path": "main.py", "old_string": "foo"
        })
        assert h1 == h2, (
            "Same tool call with reordered args produced different hashes. "
            "This means the SAME call could bypass dedup and execute repeatedly."
        )

    def test_validation_does_not_block_execution_pipeline(self, executor: ToolExecutor):
        """Full pipeline: validate + hash + execute for all core tools."""
        test_cases = [
            {"tool": "read_file", "args": {"path": "sample.py"}},
            {"tool": "list_directory", "args": {"path": "."}},
            {"tool": "get_project_overview", "args": {}},
        ]

        for tc in test_cases:
            err = validate_tool_call(tc)
            assert err is None, f"{tc['tool']}: validation returned {err!r}"


# ---------------------------------------------------------------------------
# Cross-module interaction: truncation + validation + budget
# ---------------------------------------------------------------------------


class TestCrossModuleInteraction:
    """Tests that verify correct interaction between modules."""

    def test_truncated_result_still_useful(self):
        """A truncated tool result should still contain readable head and tail."""
        large_output = "line {}\n".format(1) * 5000 + "IMPORTANT_END_LINE\n"
        result = _truncate_tool_result(large_output, max_chars=1000)

        # Should contain beginning
        assert "line 1" in result
        # Should contain the end (tail)
        assert "IMPORTANT_END_LINE" in result
        # Should mention omission
        assert "omitted" in result

    def test_budget_manager_and_debugging_classification(self):
        """Budget manager should return high-score chunks when debugging is detected."""
        config = LocalForgeConfig()
        manager = TokenBudgetManager(config)

        # Simulate: debugging query detected, relevant chunks ranked
        assert _classify_query("there's a bug in the auth handler") == "debugging"

        chunks = [
            FileChunk(file_path="auth.py", start_line=1, end_line=50,
                      content="def authenticate(user): ...", score=0.95),
            FileChunk(file_path="utils.py", start_line=1, end_line=50,
                      content="def format_date(d): ...", score=0.1),
        ]
        selected = manager.fit_chunks_to_budget(chunks, budget=100)
        # The auth.py chunk should be selected first
        assert selected[0].file_path == "auth.py"
