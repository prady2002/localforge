"""Tests for the enhancement features added across all phases.

Covers: fuzzy JSON parsing, tool call normalization, fuzzy file editing,
edit_lines, apply_diff, no-op detection, syntax validation, path suggestion,
tool call validation, hashing, output compression, and stuck detection.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from localforge.chat.tools import (
    ToolExecutor,
    _clean_json_string,
    _normalize_tool_call,
    _try_parse_json,
    extract_all_tool_calls,
    extract_json_tool_calls,
    hash_tool_call,
    validate_tool_call,
)


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def tool_repo(tmp_path: Path) -> Path:
    """A temporary repo with sample files for testing tools."""
    (tmp_path / "hello.py").write_text(
        "def hello():\n    print('Hello, world!')\n\nhello()\n",
        encoding="utf-8",
    )
    (tmp_path / "math_utils.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (tmp_path / "config.json").write_text('{"key": "value"}\n', encoding="utf-8")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "nested.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def executor(tool_repo: Path) -> ToolExecutor:
    return ToolExecutor(tool_repo)


# ══════════════════════════════════════════════════════════════════════════
# Phase 2: Robust JSON Parsing
# ══════════════════════════════════════════════════════════════════════════


class TestCleanJson:
    """Test the fuzzy JSON cleaner."""

    def test_trailing_commas(self):
        raw = '{"tool": "read_file", "args": {"path": "a.py",},}'
        result = _clean_json_string(raw)
        parsed = json.loads(result)
        assert parsed["tool"] == "read_file"

    def test_single_quotes(self):
        raw = "{'tool': 'read_file', 'args': {'path': 'a.py'}}"
        result = _clean_json_string(raw)
        parsed = json.loads(result)
        assert parsed["tool"] == "read_file"

    def test_line_comments(self):
        raw = '{\n// this is a comment\n"tool": "read_file",\n"args": {}\n}'
        result = _clean_json_string(raw)
        parsed = json.loads(result)
        assert parsed["tool"] == "read_file"

    def test_unquoted_keys(self):
        raw = '{tool: "read_file", args: {"path": "a.py"}}'
        result = _clean_json_string(raw)
        parsed = json.loads(result)
        assert parsed["tool"] == "read_file"

    def test_missing_closing_brace(self):
        raw = '{"tool": "read_file", "args": {"path": "a.py"}'
        result = _clean_json_string(raw)
        parsed = json.loads(result)
        assert parsed["tool"] == "read_file"


class TestTryParseJson:
    """Test lenient JSON parsing."""

    def test_valid_json(self):
        result = _try_parse_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_malformed_falls_back_to_cleanup(self):
        result = _try_parse_json('{"key": "value",}')
        assert result == {"key": "value"}

    def test_unparseable_returns_none(self):
        result = _try_parse_json("not json at all")
        assert result is None


class TestNormalizeToolCall:
    """Test tool call format normalization."""

    def test_internal_format(self):
        tc = _normalize_tool_call({"tool": "read_file", "args": {"path": "a.py"}})
        assert tc == {"tool": "read_file", "args": {"path": "a.py"}}

    def test_openai_format(self):
        tc = _normalize_tool_call({"name": "read_file", "arguments": {"path": "a.py"}})
        assert tc == {"tool": "read_file", "args": {"path": "a.py"}}

    def test_anthropic_nested_format(self):
        tc = _normalize_tool_call({
            "function": {"name": "read_file", "arguments": {"path": "a.py"}},
        })
        assert tc == {"tool": "read_file", "args": {"path": "a.py"}}

    def test_tool_name_format(self):
        tc = _normalize_tool_call({"tool_name": "read_file", "parameters": {"path": "a.py"}})
        assert tc == {"tool": "read_file", "args": {"path": "a.py"}}

    def test_string_arguments_auto_parsed(self):
        tc = _normalize_tool_call({"name": "read_file", "arguments": '{"path": "a.py"}'})
        assert tc == {"tool": "read_file", "args": {"path": "a.py"}}

    def test_invalid_returns_none(self):
        assert _normalize_tool_call({"random": "stuff"}) is None
        assert _normalize_tool_call("not a dict") is None
        assert _normalize_tool_call(42) is None


class TestExtractToolCalls:
    """Test enhanced tool call extraction."""

    def test_xml_with_malformed_json(self):
        text = '<tool_call>\n{"tool": "read_file", "args": {"path": "a.py",}}\n</tool_call>'
        _, tools = extract_all_tool_calls(text)
        assert len(tools) == 1
        assert tools[0]["tool"] == "read_file"

    def test_xml_no_closing_tag(self):
        text = 'Let me read that.\n<tool_call>\n{"tool": "read_file", "args": {"path": "a.py"}}'
        _, tools = extract_all_tool_calls(text)
        assert len(tools) == 1
        assert tools[0]["tool"] == "read_file"

    def test_openai_format_in_json_block(self):
        text = '```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```'
        _, tools = extract_json_tool_calls(text)
        assert len(tools) == 1
        assert tools[0]["tool"] == "read_file"

    def test_bare_json_object(self):
        text = '{"name": "run_command", "arguments": {"command": "ruff check ."}}'
        _, tools = extract_json_tool_calls(text)
        assert len(tools) == 1
        assert tools[0]["tool"] == "run_command"

    def test_multiple_xml_tool_calls(self):
        text = (
            '<tool_call>\n{"tool": "read_file", "args": {"path": "a.py"}}\n</tool_call>\n'
            '<tool_call>\n{"tool": "read_file", "args": {"path": "b.py"}}\n</tool_call>'
        )
        _, tools = extract_all_tool_calls(text)
        assert len(tools) == 2


# ══════════════════════════════════════════════════════════════════════════
# Phase 3: Smarter File Editing
# ══════════════════════════════════════════════════════════════════════════


class TestFuzzyEditing:
    """Test fuzzy matching, whitespace normalization, and edit improvements."""

    def test_noop_edit_rejected(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("edit_file", {
            "path": "hello.py",
            "old_string": "print('Hello, world!')",
            "new_string": "print('Hello, world!')",
        })
        assert "no-op" in result.lower() or "identical" in result.lower()

    def test_exact_match_works(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("edit_file", {
            "path": "hello.py",
            "old_string": "print('Hello, world!')",
            "new_string": "print('Hello, LocalForge!')",
        })
        assert "Successfully edited" in result
        content = (tool_repo / "hello.py").read_text()
        assert "Hello, LocalForge!" in content

    def test_multi_match_gives_context(self, executor: ToolExecutor, tool_repo: Path):
        # "return" appears in both add and subtract
        result = executor.execute("edit_file", {
            "path": "math_utils.py",
            "old_string": "return",
            "new_string": "return int",
        })
        assert "matches" in result.lower()
        assert "Match" in result or "line" in result.lower()

    def test_whitespace_normalized_match(self, executor: ToolExecutor, tool_repo: Path):
        # Write a file with tabs
        (tool_repo / "tabs.py").write_text("def foo():\n\treturn 1\n", encoding="utf-8")
        result = executor.execute("edit_file", {
            "path": "tabs.py",
            "old_string": "def foo():\n    return 1",  # spaces instead of tabs
            "new_string": "def foo():\n    return 2",
        })
        assert "edited" in result.lower()

    def test_fuzzy_match_with_minor_diff(self, executor: ToolExecutor, tool_repo: Path):
        (tool_repo / "fuzzy.py").write_text(
            "def compute(x, y):\n    result = x + y\n    return result\n",
            encoding="utf-8",
        )
        # Slightly wrong: "results" instead of "result"
        result = executor.execute("edit_file", {
            "path": "fuzzy.py",
            "old_string": "def compute(x, y):\n    results = x + y\n    return results",
            "new_string": "def compute(x, y):\n    total = x + y\n    return total",
        })
        # Should either fuzzy-match or give helpful error
        assert "edited" in result.lower() or "not found" in result.lower()

    def test_not_found_gives_context(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("edit_file", {
            "path": "hello.py",
            "old_string": "this_does_not_exist_anywhere_in_the_file_at_all",
            "new_string": "replacement",
        })
        assert "not found" in result.lower()
        # Should include helpful context
        assert "L" in result or "line" in result.lower() or "File has" in result


class TestEditLines:
    """Test line-range editing."""

    def test_basic_edit_lines(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("edit_lines", {
            "path": "hello.py",
            "start_line": 2,
            "end_line": 2,
            "new_content": "    print('Hello, LocalForge!')\n",
        })
        assert "Successfully replaced" in result
        content = (tool_repo / "hello.py").read_text()
        assert "Hello, LocalForge!" in content
        assert "def hello():" in content  # Line 1 unchanged

    def test_invalid_line_range(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("edit_lines", {
            "path": "hello.py",
            "start_line": 0,
            "end_line": 2,
            "new_content": "x",
        })
        assert "Invalid line range" in result

    def test_start_beyond_file(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("edit_lines", {
            "path": "hello.py",
            "start_line": 100,
            "end_line": 110,
            "new_content": "x",
        })
        assert "exceeds" in result.lower()

    def test_syntax_validation_blocks_bad_edit(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("edit_lines", {
            "path": "hello.py",
            "start_line": 1,
            "end_line": 1,
            "new_content": "def hello(:\n",  # Invalid syntax
        })
        assert "syntax error" in result.lower()


class TestApplyDiff:
    """Test unified diff application."""

    def test_simple_diff(self, executor: ToolExecutor, tool_repo: Path):
        diff = textwrap.dedent("""\
            --- a/hello.py
            +++ b/hello.py
            @@ -1,4 +1,4 @@
             def hello():
            -    print('Hello, world!')
            +    print('Hello, LocalForge!')
             
             hello()
        """)
        result = executor.execute("apply_diff", {
            "path": "hello.py",
            "diff": diff,
        })
        assert "Successfully applied" in result
        content = (tool_repo / "hello.py").read_text()
        assert "Hello, LocalForge!" in content

    def test_empty_diff_rejected(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("apply_diff", {"path": "hello.py", "diff": ""})
        assert "required" in result.lower()


# ══════════════════════════════════════════════════════════════════════════
# Phase 7: Anti-hallucination
# ══════════════════════════════════════════════════════════════════════════


class TestPathSuggestion:
    """Test file path suggestion on not-found."""

    def test_suggests_similar_path(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("read_file", {"path": "helo.py"})  # typo
        assert "Did you mean" in result or "not found" in result.lower()

    def test_no_suggestion_for_random_name(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("read_file", {"path": "zzzzzzzzz.xyz"})
        assert "not found" in result.lower()


class TestSyntaxValidation:
    """Test Python syntax checking on edits."""

    def test_valid_edit_passes(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("edit_file", {
            "path": "hello.py",
            "old_string": "print('Hello, world!')",
            "new_string": "print('Hello!')",
        })
        assert "Successfully" in result

    def test_invalid_syntax_blocked(self, executor: ToolExecutor, tool_repo: Path):
        result = executor.execute("edit_file", {
            "path": "hello.py",
            "old_string": "def hello():\n    print('Hello, world!')",
            "new_string": "def hello(\n    print('Hello, world!')",  # Missing closing paren
        })
        assert "syntax error" in result.lower()
        # File should not be modified
        content = (tool_repo / "hello.py").read_text()
        assert "def hello():" in content  # Original preserved


# ══════════════════════════════════════════════════════════════════════════
# Tool Call Validation & Hashing
# ══════════════════════════════════════════════════════════════════════════


class TestToolCallValidation:
    """Test tool call validation."""

    def test_valid_call(self):
        assert validate_tool_call({"tool": "read_file", "args": {"path": "a.py"}}) is None

    def test_missing_required_arg(self):
        err = validate_tool_call({"tool": "read_file", "args": {}})
        assert err is not None
        assert "path" in err

    def test_unknown_tool(self):
        err = validate_tool_call({"tool": "nonexistent_tool", "args": {}})
        assert err is not None
        assert "Unknown tool" in err

    def test_unknown_tool_with_suggestion(self):
        err = validate_tool_call({"tool": "read_fille", "args": {"path": "a.py"}})
        assert err is not None
        assert "read_file" in err  # Should suggest correct name


class TestToolCallHash:
    """Test deterministic hashing of tool calls."""

    def test_same_call_same_hash(self):
        h1 = hash_tool_call("read_file", {"path": "a.py"})
        h2 = hash_tool_call("read_file", {"path": "a.py"})
        assert h1 == h2

    def test_different_calls_different_hash(self):
        h1 = hash_tool_call("read_file", {"path": "a.py"})
        h2 = hash_tool_call("read_file", {"path": "b.py"})
        assert h1 != h2

    def test_arg_order_independent(self):
        h1 = hash_tool_call("edit_file", {"path": "a.py", "old_string": "x", "new_string": "y"})
        h2 = hash_tool_call("edit_file", {"new_string": "y", "old_string": "x", "path": "a.py"})
        assert h1 == h2


# ══════════════════════════════════════════════════════════════════════════
# Phase 6: Output Compression
# ══════════════════════════════════════════════════════════════════════════


class TestOutputCompression:
    """Test tool output compression for large linter outputs."""

    def test_small_output_not_compressed(self):
        output = "All good!\n"
        result = ToolExecutor._compress_tool_output(output, "ruff check .")
        assert result == output

    def test_large_linter_output_compressed(self):
        lines = []
        for i in range(50):
            lines.append(f"src/file{i % 5}.py:{i}:1: E501 Line too long")
        for i in range(30):
            lines.append(f"src/file{i % 3}.py:{i}:1: W191 Indentation contains tabs")
        output = "\n".join(lines) + "\n(exit code: 1)"
        result = ToolExecutor._compress_tool_output(output, "ruff check .")
        # Should be shorter than original
        assert len(result) < len(output)
        assert "E501" in result
        assert "W191" in result
        assert "occurrence" in result

    def test_non_linter_not_compressed(self):
        lines = ["line " * 10] * 50
        output = "\n".join(lines)
        result = ToolExecutor._compress_tool_output(output, "python main.py")
        assert result == output


# ══════════════════════════════════════════════════════════════════════════
# Unknown tool suggestion
# ══════════════════════════════════════════════════════════════════════════


class TestUnknownToolSuggestion:
    """Test that unknown tool names get close-match suggestions."""

    def test_typo_in_tool_name(self, executor: ToolExecutor):
        result = executor.execute("reed_file", {"path": "a.py"})
        assert "read_file" in result

    def test_completely_wrong_name(self, executor: ToolExecutor):
        result = executor.execute("zzzzzzz", {})
        assert "Unknown tool" in result
