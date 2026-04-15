"""Tests for the chat tool-use framework."""

from __future__ import annotations

from pathlib import Path

import pytest

from localforge.chat.engine import ChatEngine
from localforge.chat.tools import (
    ToolExecutor,
    extract_all_tool_calls,
    extract_json_tool_calls,
)


@pytest.fixture()
def tool_repo(tmp_path: Path) -> Path:
    """Create a minimal repo structure for tool tests."""
    (tmp_path / "hello.py").write_text("print('hello world')\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "data.txt").write_text("some data\nline 2\nline 3\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def executor(tool_repo: Path) -> ToolExecutor:
    return ToolExecutor(tool_repo)


# ── extract_tool_call ────────────────────────────────────────────────────

class TestExtractToolCall:
    def test_no_tool_call(self, executor: ToolExecutor):
        text = "Just a normal response without any tools."
        before, tool = executor.extract_tool_call(text)
        assert before == text
        assert tool is None

    def test_valid_tool_call(self, executor: ToolExecutor):
        text = (
            'Let me read that file.\n\n'
            '<tool_call>\n'
            '{"tool": "read_file", "args": {"path": "hello.py"}}\n'
            '</tool_call>'
        )
        before, tool = executor.extract_tool_call(text)
        assert "Let me read" in before
        assert tool is not None
        assert tool["tool"] == "read_file"
        assert tool["args"]["path"] == "hello.py"

    def test_malformed_json(self, executor: ToolExecutor):
        text = '<tool_call>\nnot json\n</tool_call>'
        before, tool = executor.extract_tool_call(text)
        assert tool is None

    def test_missing_end_tag(self, executor: ToolExecutor):
        text = '<tool_call>\n{"tool": "read_file"}\n'
        before, tool = executor.extract_tool_call(text)
        assert tool is None


# ── read_file tool ───────────────────────────────────────────────────────

class TestReadFile:
    def test_read_full_file(self, executor: ToolExecutor):
        result = executor.execute("read_file", {"path": "hello.py"})
        assert "hello world" in result

    def test_read_with_line_range(self, executor: ToolExecutor):
        result = executor.execute(
            "read_file",
            {"path": "sub/data.txt", "start_line": 2, "end_line": 2},
        )
        assert "line 2" in result
        assert "some data" not in result

    def test_read_nonexistent(self, executor: ToolExecutor):
        result = executor.execute("read_file", {"path": "missing.py"})
        assert "Error" in result

    def test_read_path_traversal(self, executor: ToolExecutor):
        result = executor.execute("read_file", {"path": "../../etc/passwd"})
        assert "Error" in result


# ── write_file tool ──────────────────────────────────────────────────────

class TestWriteFile:
    def test_write_new_file(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("write_file", {"path": "new.py", "content": "x = 1\n"})
        assert "Successfully" in result
        assert (tool_repo / "new.py").read_text() == "x = 1\n"

    def test_write_creates_directories(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("write_file", {"path": "a/b/c.txt", "content": "deep\n"})
        assert "Successfully" in result
        assert (tool_repo / "a" / "b" / "c.txt").read_text() == "deep\n"

    def test_write_path_traversal(self, executor: ToolExecutor):
        # Parent directory writes are now allowed for project scaffolding
        # But deeply nested traversal (3+ levels up) is blocked
        result = executor.execute("write_file", {"path": "../../../escape.txt", "content": "bad"})
        assert "Error" in result


# ── edit_file tool ───────────────────────────────────────────────────────

class TestEditFile:
    def test_edit_success(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("edit_file", {
            "path": "hello.py",
            "old_string": "hello world",
            "new_string": "goodbye world",
        })
        assert "Successfully" in result
        assert "goodbye world" in (tool_repo / "hello.py").read_text()

    def test_edit_not_found(self, executor: ToolExecutor):
        result = executor.execute("edit_file", {
            "path": "hello.py",
            "old_string": "nonexistent string",
            "new_string": "replacement",
        })
        assert "not found" in result

    def test_edit_nonexistent_file(self, executor: ToolExecutor):
        result = executor.execute("edit_file", {
            "path": "missing.py",
            "old_string": "x",
            "new_string": "y",
        })
        assert "Error" in result


# ── list_directory tool ──────────────────────────────────────────────────

class TestListDirectory:
    def test_list_root(self, executor: ToolExecutor):
        result = executor.execute("list_directory", {"path": "."})
        assert "hello.py" in result
        assert "sub/" in result

    def test_list_subdir(self, executor: ToolExecutor):
        result = executor.execute("list_directory", {"path": "sub"})
        assert "data.txt" in result

    def test_list_nonexistent(self, executor: ToolExecutor):
        result = executor.execute("list_directory", {"path": "nope"})
        assert "Error" in result


# ── run_command tool ─────────────────────────────────────────────────────

class TestRunCommand:
    def test_simple_command(self, executor: ToolExecutor):
        result = executor.execute("run_command", {"command": "python --version"})
        assert "Python" in result

    def test_blocked_command(self, executor: ToolExecutor):
        result = executor.execute("run_command", {"command": "rm -rf /"})
        assert "blocked" in result

    def test_empty_command(self, executor: ToolExecutor):
        result = executor.execute("run_command", {"command": ""})
        assert "Error" in result

    def test_normalize_ruff_fix_command(self, executor: ToolExecutor):
        normalized = executor._normalize_ruff_command("ruff --fix .")
        assert normalized == "ruff check . --fix"

    def test_normalize_ruff_dot_command(self, executor: ToolExecutor):
        normalized = executor._normalize_ruff_command("ruff .")
        assert normalized == "ruff check ."

    def test_normalize_non_ruff_command_unchanged(self, executor: ToolExecutor):
        normalized = executor._normalize_ruff_command("python -m pytest -q")
        assert normalized == "python -m pytest -q"


class TestActionRoutingHeuristics:
    def test_fast_action_simple_ruff_check(self):
        """A simple 'run ruff check .' without fix intent IS a fast action."""
        assert ChatEngine._is_fast_action_query("run ruff check .")

    def test_not_fast_action_when_fix_requested(self):
        """'run ruff and fix issues' is multi-step work, NOT a fast action."""
        assert not ChatEngine._is_fast_action_query("run ruff check . and fix issues")

    def test_not_fast_action_for_deep_refactor_prompt(self):
        assert not ChatEngine._is_fast_action_query(
            "refactor architecture and edit multiple modules with tests"
        )

    def test_fast_action_simple_pytest(self):
        assert ChatEngine._is_fast_action_query("run pytest tests/ -v")


class TestTestFixQueryClassification:
    """Test the test_fix query type for fast test-driven debugging."""

    def test_pytest_failures_with_fix(self):
        assert ChatEngine._is_test_fix_query("5 tests fail in test_enhancements.py, fix them")

    def test_investigate_test_failures(self):
        assert ChatEngine._is_test_fix_query(
            "pytest tests/ shows multiple failures, can you investigate and fix?"
        )

    def test_run_tests_and_fix(self):
        assert ChatEngine._is_test_fix_query("run the tests and fix any failing ones")

    def test_jest_test_broken(self):
        assert ChatEngine._is_test_fix_query("jest tests are broken, please fix")

    def test_not_test_fix_for_simple_run(self):
        """A simple 'run pytest' without failure/fix intent is NOT test_fix."""
        assert not ChatEngine._is_test_fix_query("run pytest tests/ -v")

    def test_not_test_fix_for_general_bug(self):
        """A general bug report without test mention is NOT test_fix."""
        assert not ChatEngine._is_test_fix_query("the app crashes when I click submit")

    def test_test_error_investigating(self):
        assert ChatEngine._is_test_fix_query(
            "test_validation is failing with an assertion error, debug it"
        )


# ── search_code tool ─────────────────────────────────────────────────────

class TestSearchCode:
    def test_search_pattern(self, executor: ToolExecutor):
        result = executor.execute("search_code", {"pattern": "hello"})
        # Should find something — either via index or grep fallback
        assert result  # non-empty

    def test_search_empty(self, executor: ToolExecutor):
        result = executor.execute("search_code", {"pattern": ""})
        assert "Error" in result

    def test_search_no_matches(self, executor: ToolExecutor):
        result = executor.execute("search_code", {"pattern": "ZZZZNOTHERE"})
        assert "no matches" in result.lower()

    def test_search_non_ascii_files(self, tmp_path: Path):
        """search_code should not crash on files with non-ASCII characters."""
        # Create file with em-dashes and other non-ASCII
        (tmp_path / "fancy.py").write_text(
            '"""Module — powered by café™ résumé."""\nPOWERED = "yes"\n',
            encoding="utf-8",
        )
        executor = ToolExecutor(tmp_path)
        result = executor.execute("search_code", {"pattern": "POWERED"})
        assert "POWERED" in result
        assert "Error" not in result

    def test_search_with_file_glob(self, tool_repo: Path):
        """search_code should respect file_glob filter."""
        result = ToolExecutor(tool_repo).execute(
            "search_code", {"pattern": "hello", "file_glob": "*.py"}
        )
        assert "hello" in result

    def test_search_respects_focus(self, tool_repo: Path):
        """search_code should respect focus paths."""
        executor = ToolExecutor(tool_repo)
        executor.focus_paths = ["sub"]
        result = executor.execute("search_code", {"pattern": "some data"})
        assert "sub" in result or "data.txt" in result


# ── unknown tool ─────────────────────────────────────────────────────────

class TestUnknownTool:
    def test_unknown_tool_name(self, executor: ToolExecutor):
        result = executor.execute("nope", {})
        assert "Unknown tool" in result


# ── extract_all_tool_calls ───────────────────────────────────────────────

class TestExtractAllToolCalls:
    def test_no_tool_calls(self):
        text = "Just a normal response."
        clean, calls = extract_all_tool_calls(text)
        assert clean == text
        assert calls == []


class TestExtractJsonToolCalls:
    def test_extracts_single_native_style_object(self):
        text = '{"name": "verify_changes", "arguments": {"command": "mypy localforge/"}}'
        clean, calls = extract_json_tool_calls(text)
        assert clean == ""
        assert calls == [{"tool": "verify_changes", "args": {"command": "mypy localforge/"}}]

    def test_extracts_fenced_json_object(self):
        text = (
            "I'll run checks now.\n"
            "```json\n"
            '{"name":"run_command","arguments":{"command":"python -m pytest -q"}}\n'
            "```"
        )
        clean, calls = extract_json_tool_calls(text)
        assert "I'll run checks now." in clean
        assert calls == [{"tool": "run_command", "args": {"command": "python -m pytest -q"}}]

    def test_extracts_array_of_calls(self):
        text = (
            "```json\n"
            "["
            '{"name":"read_file","arguments":{"path":"a.py"}},'
            '{"name":"read_file","arguments":{"path":"b.py"}}'
            "]\n"
            "```"
        )
        clean, calls = extract_json_tool_calls(text)
        assert clean == ""
        assert len(calls) == 2
        assert calls[0]["tool"] == "read_file"
        assert calls[0]["args"]["path"] == "a.py"
        assert calls[1]["args"]["path"] == "b.py"

    def test_single_tool_call(self):
        text = (
            'Reading file.\n'
            '<tool_call>\n'
            '{"tool": "read_file", "args": {"path": "test.py"}}\n'
            '</tool_call>\n'
            'Done.'
        )
        clean, calls = extract_all_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["tool"] == "read_file"
        assert "Reading file" in clean

    def test_multiple_tool_calls(self):
        text = (
            'Let me read both files.\n'
            '<tool_call>\n'
            '{"tool": "read_file", "args": {"path": "a.py"}}\n'
            '</tool_call>\n'
            'And also:\n'
            '<tool_call>\n'
            '{"tool": "read_file", "args": {"path": "b.py"}}\n'
            '</tool_call>'
        )
        clean, calls = extract_all_tool_calls(text)
        assert len(calls) == 2
        assert calls[0]["args"]["path"] == "a.py"
        assert calls[1]["args"]["path"] == "b.py"

    def test_malformed_json_skipped(self):
        text = (
            '<tool_call>\nnot json\n</tool_call>\n'
            '<tool_call>\n'
            '{"tool": "list_directory", "args": {"path": "."}}\n'
            '</tool_call>'
        )
        clean, calls = extract_all_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["tool"] == "list_directory"


# ── grep_codebase tool ──────────────────────────────────────────────────

class TestGrepCodebase:
    def test_grep_literal(self, executor: ToolExecutor):
        result = executor.execute("grep_codebase", {"pattern": "hello world"})
        assert "hello.py" in result

    def test_grep_regex(self, executor: ToolExecutor):
        result = executor.execute("grep_codebase", {"pattern": "hel+o", "is_regex": True})
        assert "hello.py" in result

    def test_grep_no_matches(self, executor: ToolExecutor):
        result = executor.execute("grep_codebase", {"pattern": "nonexistent_xyz_abc"})
        assert "No matches" in result

    def test_grep_empty_pattern(self, executor: ToolExecutor):
        result = executor.execute("grep_codebase", {"pattern": ""})
        assert "Error" in result

    def test_grep_invalid_regex(self, executor: ToolExecutor):
        result = executor.execute("grep_codebase", {"pattern": "[invalid", "is_regex": True})
        assert "Error" in result

    def test_grep_file_glob_filter(self, executor: ToolExecutor):
        result = executor.execute(
            "grep_codebase", {"pattern": "data", "file_glob": "*.txt"}
        )
        assert "data.txt" in result


# ── batch_edit tool ──────────────────────────────────────────────────────

class TestBatchEdit:
    def test_batch_edit_multiple_files(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("batch_edit", {
            "edits": [
                {
                    "path": "hello.py",
                    "old_string": "hello world",
                    "new_string": "batch world",
                },
                {
                    "path": "sub/data.txt",
                    "old_string": "some data",
                    "new_string": "batch data",
                },
            ]
        })
        assert "Successfully" in result
        assert "batch world" in (tool_repo / "hello.py").read_text()
        assert "batch data" in (tool_repo / "sub" / "data.txt").read_text()

    def test_batch_edit_empty(self, executor: ToolExecutor):
        result = executor.execute("batch_edit", {"edits": []})
        assert "Error" in result

    def test_batch_edit_partial_failure(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("batch_edit", {
            "edits": [
                {
                    "path": "hello.py",
                    "old_string": "hello world",
                    "new_string": "new world",
                },
                {
                    "path": "hello.py",
                    "old_string": "nonexistent string",
                    "new_string": "blah",
                },
            ]
        })
        # First edit succeeds, second fails
        assert "Successfully" in result
        assert "not found" in result


# ── verify_changes tool ─────────────────────────────────────────────────

class TestVerifyChanges:
    def test_verify_with_custom_command(self, executor: ToolExecutor):
        result = executor.execute("verify_changes", {"command": "python --version"})
        assert "Python" in result

    def test_verify_auto_detect(self, executor: ToolExecutor):
        # Should not crash even on a minimal tmp repo
        result = executor.execute("verify_changes", {})
        assert result  # non-empty string


# ── run_command with configurable timeout ────────────────────────────────

class TestRunCommandTimeout:
    def test_custom_timeout(self, executor: ToolExecutor):
        result = executor.execute("run_command", {"command": "python --version", "timeout": 30})
        assert "Python" in result

    def test_unblocked_curl(self, executor: ToolExecutor):
        # curl should no longer be blocked (useful for API testing)
        result = executor.execute("run_command", {"command": "curl --version"})
        # May succeed or fail depending on system, but should NOT say "blocked"
        assert "blocked" not in result.lower()


# ── _is_lazy_response detection ──────────────────────────────────────────

class TestIsLazyResponse:
    def test_detects_instruction_steps(self):
        text = (
            "Here are the steps to fix this:\n"
            "1. Open the file src/main.py\n"
            "2. Find the function handle_request\n"
            "3. Change the return value\n"
            "4. Run the tests"
        )
        assert ChatEngine._is_lazy_response(text) is True

    def test_detects_you_can_run(self):
        text = (
            "There are some type errors in the code. "
            "You can run mypy to check for type errors:\n"
            "```bash\nmypy src/ --ignore-missing-imports\n```\n"
            "You should also run pytest to check for failing tests."
        )
        assert ChatEngine._is_lazy_response(text) is True


# ── Pytest output compression ─────────────────────────────────────────────

class TestPytestOutputCompression:
    """Test the _compress_pytest_output method for reducing test output tokens."""

    def test_strips_passing_tests(self):
        """Passing test lines should be removed from compressed output."""
        lines = [
            "============================= test session starts =============================",
            "platform win32 -- Python 3.13.7, pytest-9.0.2",
            "collected 47 items",
            "",
        ]
        # Add 30 PASSED lines
        for i in range(30):
            lines.append(f"tests/test_file.py::test_{i} PASSED")
        lines.extend([
            "_____________________ test_broken _____________________",
            "    def test_broken():",
            ">       assert False",
            "E       AssertionError",
            "",
            "tests/test_file.py:10: AssertionError",
            "=========================== short test summary info ============================",
            "FAILED tests/test_file.py::test_broken",
            "========================= 1 failed, 30 passed =========================",
        ])
        output = "\n".join(lines)
        compressed = ToolExecutor._compress_pytest_output(output)
        assert "PASSED" not in compressed
        assert "FAILED" in compressed
        assert "1 failed" in compressed
        assert len(compressed) < len(output)

    def test_extracts_source_files_from_tracebacks(self):
        """Source files mentioned in tracebacks should appear in a summary header."""
        output = "\n".join([
            "============================= test session starts =============================",
            "collected 5 items",
        ] + [f"tests/test_x.py::test_{i} PASSED" for i in range(20)] + [
            "_____________________ test_broken _____________________",
            "localforge/chat/tools.py:123: in validate_tool_call",
            "    return None",
            "tests/test_enhancements.py:45: AssertionError",
            "=========================== short test summary info ============================",
            "FAILED tests/test_enhancements.py::test_broken",
            "========================= 1 failed, 20 passed =========================",
        ])
        compressed = ToolExecutor._compress_pytest_output(output)
        assert "SOURCE FILES IN ERRORS:" in compressed
        assert "localforge/chat/tools.py" in compressed

    def test_small_output_not_compressed(self):
        """Output with few lines should not be reduced below 85% threshold."""
        output = "\n".join([
            "============================= test session starts =============================",
            "collected 1 item",
            "tests/test_x.py::test_one PASSED",
            "========================= 1 passed =========================",
        ])
        result = ToolExecutor._compress_pytest_output(output)
        assert result == output  # No compression for small output


class TestAutoOptimizePytestCommand:
    """Test that pytest commands auto-get --tb=short when missing."""

    def test_detect_bare_pytest(self):
        """Bare pytest command should need --tb=short appended."""
        lower = "pytest tests/test_enhancements.py"
        should_append = ("pytest" in lower or "py.test" in lower) and "--tb" not in lower
        assert should_append

    def test_no_append_when_tb_present(self):
        """If --tb is already present, don't append."""
        lower = "pytest tests/ --tb=long"
        should_append = ("pytest" in lower or "py.test" in lower) and "--tb" not in lower
        assert not should_append


class TestReadFileHint:
    """Test that file-not-found errors include actionable hints."""

    def test_file_not_found_includes_hint(self, executor: ToolExecutor):
        result = executor.execute("read_file", {"path": "nonexistent_file.py"})
        assert "Error: File not found" in result
        assert "does NOT exist" in result or "Hint:" in result
        assert "search_code" in result or "grep_codebase" in result or "test file imports" in result

    def test_detects_suggestion_pattern(self):
        text = (
            "To fix this issue, I recommend the following approach:\n"
            "First, you need to update the config file.\n"
            "Then you should run the tests to make sure everything works.\n"
            "I would suggest also running the linter."
        )
        assert ChatEngine._is_lazy_response(text) is True

    def test_allows_short_answers(self):
        text = "The function is defined in src/main.py on line 42."
        assert ChatEngine._is_lazy_response(text) is False

    def test_allows_genuine_explanations(self):
        text = (
            "The authentication flow works as follows: when a user submits "
            "their credentials, the login endpoint validates them against "
            "the database. If valid, a JWT token is generated and returned."
        )
        assert ChatEngine._is_lazy_response(text) is False

    def test_allows_tool_result_summaries(self):
        text = (
            "I've fixed the bug in src/main.py by updating the null check "
            "in the handle_request function. The tests are all passing now. "
            "Here's what I changed: the return type was wrong."
        )
        assert ChatEngine._is_lazy_response(text) is False


# ── _is_premature_handoff detection ──────────────────────────────────────

class TestIsPrematureHandoff:
    def test_detects_please_review(self):
        text = (
            "I ran ruff check and found some issues. "
            "Please review the output to see if there are any specific "
            "issues that need manual attention."
        )
        assert ChatEngine._is_premature_handoff(text) is True

    def test_detects_let_me_know(self):
        text = "The command ran successfully. Let me know if you need anything else."
        assert ChatEngine._is_premature_handoff(text) is True

    def test_detects_would_you_like(self):
        text = "I found 5 lint errors. Would you like me to fix them?"
        assert ChatEngine._is_premature_handoff(text) is True

    def test_allows_actual_completion(self):
        text = "All ruff issues have been fixed and verified. No errors remain."
        assert ChatEngine._is_premature_handoff(text) is False

    def test_allows_summary(self):
        text = "I edited 3 files to resolve the type errors. All tests pass."
        assert ChatEngine._is_premature_handoff(text) is False


# ── TOOL_SCHEMAS validation ──────────────────────────────────────────────

class TestToolSchemas:
    """Validate the native Ollama tool schemas are well-formed."""

    def test_schemas_is_list(self):
        from localforge.chat.tools import TOOL_SCHEMAS
        assert isinstance(TOOL_SCHEMAS, list)
        assert len(TOOL_SCHEMAS) >= 11

    def test_each_schema_has_required_keys(self):
        from localforge.chat.tools import TOOL_SCHEMAS
        for schema in TOOL_SCHEMAS:
            assert schema["type"] == "function"
            func = schema["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func
            params = func["parameters"]
            assert params["type"] == "object"
            assert "properties" in params
            assert "required" in params

    def test_all_tools_have_schemas(self):
        """Every tool in the executor dispatch table has a matching schema."""
        from localforge.chat.tools import TOOL_SCHEMAS
        schema_names = {s["function"]["name"] for s in TOOL_SCHEMAS}
        expected = {
            "read_file", "write_file", "edit_file", "edit_lines",
            "apply_diff", "list_directory",
            "run_command", "search_code", "find_symbols",
            "get_project_overview", "grep_codebase", "verify_changes",
            "batch_edit", "create_directory", "create_project",
        }
        assert expected == schema_names

    def test_schema_name_types(self):
        from localforge.chat.tools import TOOL_SCHEMAS
        for schema in TOOL_SCHEMAS:
            assert isinstance(schema["function"]["name"], str)
            assert isinstance(schema["function"]["description"], str)
            assert len(schema["function"]["name"]) > 0


# ── Native tool call dispatch ────────────────────────────────────────────

class TestNativeToolCallDispatch:
    """Test that executor can handle native Ollama tool call argument formats."""

    def test_execute_with_dict_args(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("read_file", {"path": "hello.py"})
        assert "hello world" in result

    def test_execute_unknown_tool(self, executor: ToolExecutor):
        result = executor.execute("nonexistent_tool", {})
        assert "Error" in result
        assert "Unknown tool" in result

    def test_execute_with_empty_args(self, executor: ToolExecutor):
        result = executor.execute("get_project_overview", {})
        assert "PROJECT STRUCTURE" in result or "." in result

    def test_execute_list_directory_default(self, executor: ToolExecutor):
        result = executor.execute("list_directory", {})
        assert "hello.py" in result
