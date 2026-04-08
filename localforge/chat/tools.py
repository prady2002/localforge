"""Tool definitions and executor for the chat engine's tool-use loop.

The LLM can invoke these tools by outputting a JSON block with a ``tool``
key.  The tool executor runs the request, captures the result, and returns
it as a string to be fed back to the model.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import logging
import os
import re as _re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maximum bytes of stdout/stderr captured from shell commands.
_MAX_OUTPUT = 50_000

# Shell commands that are always blocked (case-insensitive first token).
_BLOCKED_COMMANDS = frozenset({
    "rm", "rmdir", "del", "format", "mkfs", "dd",
    "shutdown", "reboot", "poweroff", "halt",
})

# Fuzzy matching threshold for edit_file fallback
_FUZZY_MATCH_THRESHOLD = 0.85

# Max lines to show as context when edit fails
_EDIT_ERROR_CONTEXT_LINES = 15


# ── Tool schemas (for the system prompt) ─────────────────────────────────

TOOL_DESCRIPTIONS = """\
You have access to the following tools. To use a tool, you MUST output a JSON
block wrapped in <tool_call> tags EXACTLY like this:

<tool_call>
{"tool": "tool_name", "args": {"arg1": "value1"}}
</tool_call>

You can call MULTIPLE tools in one response by including multiple <tool_call> blocks.
After each round of tool calls, you will receive ALL results and can continue.

EXAMPLES — follow these EXACTLY:

Example 1 — Reading a file:
<tool_call>
{"tool": "read_file", "args": {"path": "src/main.py"}}
</tool_call>

Example 2 — Running a command:
<tool_call>
{"tool": "run_command", "args": {"command": "python -m mypy src/ --ignore-missing-imports"}}
</tool_call>

Example 3 — Editing a file:
<tool_call>
{"tool": "edit_file", "args": {
  "path": "src/main.py",
  "old_string": "def old_func():",
  "new_string": "def new_func()"
}}
</tool_call>

Example 4 — Multiple tools in one response:
I'll read both files now.
<tool_call>
{"tool": "read_file", "args": {"path": "src/a.py"}}
</tool_call>
<tool_call>
{"tool": "read_file", "args": {"path": "src/b.py"}}
</tool_call>

Available tools:

1. **read_file** — Read the contents of a file.
   args: {"path": "relative/path/to/file", "start_line": 1, "end_line": 50}
   (start_line and end_line are optional; omit to read the full file)

2. **write_file** — Write content to a file (creates or overwrites).
   args: {"path": "relative/path/to/file", "content": "file content here"}

3. **edit_file** — Replace a specific string in a file.
   args: {"path": "relative/path/to/file",
          "old_string": "text to find",
          "new_string": "replacement text"}

4. **list_directory** — List files and folders in a directory (with file sizes).
   args: {"path": "relative/path"} (omit path or use "." for repo root)

5. **run_command** — Run a shell command and get the output.
   args: {"command": "pytest tests/ -q", "timeout": 120}
   (Destructive commands like rm, del, format are blocked for safety.)
   (timeout is optional, default 120 seconds. Use higher for long builds.)

6. **search_code** — Search the codebase for a text pattern.
   args: {"pattern": "search string", "file_glob": "*.py"}
   (file_glob is optional)

7. **find_symbols** — Find function/class/variable definitions by name.
   args: {"name": "symbol_name", "kind": "function"}
   (kind is optional: function, class, variable, constant, interface)

8. **get_project_overview** — Get a high-level overview of the project structure,
   key files, and architecture. No args needed.
   args: {}

9. **grep_codebase** — Powerful recursive grep across the entire codebase.
   args: {"pattern": "regex_or_string", "file_glob": "*.py", "is_regex": true}
   (file_glob and is_regex are optional. Defaults: all files, literal search)
   Returns matching lines with file paths and line numbers.

10. **verify_changes** — Run the project's test suite, linter, and type checker
    to verify your changes work correctly.
    args: {"command": "pytest tests/ -v"}
    (command is optional — if omitted, auto-detects and runs all checks)

11. **batch_edit** — Make multiple edits across one or more files at once.
    args: {"edits": [{"path": "file.py", "old_string": "old", "new_string": "new"}, ...]}
    Returns results for each edit.

12. **edit_lines** — Replace lines by line number range (avoids string matching issues).
    args: {"path": "file.py", "start_line": 10, "end_line": 15, "new_content": "replacement code"}
    start_line/end_line are 1-based inclusive. Use this when you know exact line numbers.

13. **apply_diff** — Apply a unified diff to a file.
    args: {"path": "file.py", "diff": "--- a/file.py\n+++ b/file.py\n@@ -10,3 +10,4 @@\n..."}
    Use standard unified diff format. Useful for complex multi-hunk changes.

ABSOLUTE RULES — YOU MUST FOLLOW THESE:
- NEVER describe steps for the user to do. YOU do everything yourself using tools.
- If the user asks you to run mypy, YOU run it with run_command. DO NOT tell the user to run it.
- If the user asks you to fix a bug, YOU find the code, fix it, and verify. DO NOT give instructions.
- YOU are the agent. YOU execute. YOU do not delegate to the human.
- ALWAYS use <tool_call> tags to invoke tools. NEVER just mention a tool without calling it.
- After making code changes, ALWAYS run verify_changes or run_command to test.
- If tests fail, analyze the error, fix it with edit_file, and re-run tests. KEEP GOING.
- For every user request: ACT first, EXPLAIN after.
- You can run ANY command: pytest, mypy, ruff, npm, pip, cargo, make, go, etc.
- Ruff syntax reminder: use `ruff check .` and `ruff check . --fix`.
- On Windows, if a tool fails with "not recognized", try `python -m <tool>` instead.
- Only truly destructive commands (rm, del, format, mkfs) are blocked.
"""

# ── Native Ollama tool schemas (JSON format for tool calling API) ─────────

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents. Always read before editing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file"},
                    "start_line": {"type": "integer", "description": "Start line (1-based, optional)"},
                    "end_line": {"type": "integer", "description": "End line (1-based, optional)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file. Parent directories are created automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file"},
                    "content": {"type": "string", "description": "The content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a specific string in a file. Read file first to get exact content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file"},
                    "old_string": {"type": "string", "description": "Exact text to find and replace"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and folders in a directory with sizes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory path (default: repo root)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command (pytest, mypy, ruff, npm, pip, make, etc). Destructive commands blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 120, max 600)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search the codebase index for a text pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Search string"},
                    "file_glob": {"type": "string", "description": "Optional file pattern like *.py"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_symbols",
            "description": "Find function/class/variable definitions by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name to search for"},
                    "kind": {"type": "string", "description": "Optional: function, class, variable, constant, interface"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_overview",
            "description": "Get a high-level overview of the project structure, key files, and architecture.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_codebase",
            "description": "Recursive grep across entire codebase with optional regex.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Search pattern"},
                    "file_glob": {"type": "string", "description": "Optional file pattern"},
                    "is_regex": {"type": "boolean", "description": "Whether pattern is regex (default false)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_changes",
            "description": "Run project test suite, linter, type checker. Auto-detects if no command given.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Optional specific command to run"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_edit",
            "description": "Make multiple edits across files at once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"},
                            },
                            "required": ["path", "old_string", "new_string"],
                        },
                        "description": "List of edits to apply",
                    },
                },
                "required": ["edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_lines",
            "description": "Replace lines by line number range. Use when you know exact line numbers from read_file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file"},
                    "start_line": {"type": "integer", "description": "Start line number (1-based, inclusive)"},
                    "end_line": {"type": "integer", "description": "End line number (1-based, inclusive)"},
                    "new_content": {"type": "string", "description": "Replacement content for those lines"},
                },
                "required": ["path", "start_line", "end_line", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_diff",
            "description": "Apply a unified diff to a file. Use standard unified diff format.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file"},
                    "diff": {"type": "string", "description": "Unified diff content"},
                },
                "required": ["path", "diff"],
            },
        },
    },
]

# Lean tool set for fast-action prompts (run command, read/edit/write files).
# Sending fewer schemas drastically reduces prompt-processing time in Ollama.
TOOL_SCHEMAS_FAST = [
    s for s in TOOL_SCHEMAS
    if s["function"]["name"] in {
        "run_command", "read_file", "edit_file", "write_file", "batch_edit",
        "edit_lines", "apply_diff", "list_directory",
    }
]


# ── Robust JSON cleaning ──────────────────────────────────────────────────

def _clean_json_string(raw: str) -> str:
    """Best-effort cleanup of malformed JSON from local models.

    Handles: trailing commas, single quotes used as string delimiters,
    // comments, unquoted keys, missing closing braces, and Python-style
    triple-quoted strings.
    """
    s = raw.strip()

    # Convert Python triple-quoted strings (""" or ''') to JSON strings.
    # Models sometimes output: "new_content": """some\ncontent"""
    def _triple_to_json(m: _re.Match) -> str:
        inner = m.group(1)
        # Escape embedded double quotes and newlines for valid JSON
        escaped = inner.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
        return f'"{escaped}"'

    s = _re.sub(r'"""(.*?)"""', _triple_to_json, s, flags=_re.DOTALL)
    s = _re.sub(r"'''(.*?)'''", _triple_to_json, s, flags=_re.DOTALL)

    # Remove // and # line comments (but not inside strings — heuristic)
    s = _re.sub(r'(?m)^\s*//.*$', '', s)
    s = _re.sub(r'(?m)^\s*#.*$', '', s)

    # Remove trailing commas before } or ]
    s = _re.sub(r',\s*([}\]])', r'\1', s)

    # Replace single-quoted strings with double-quoted (crude but effective)
    # Only do this if there are no double-quoted strings (avoid mixing)
    if "'" in s and '"' not in s:
        s = s.replace("'", '"')

    # Try to fix unquoted keys: word: -> "word":
    s = _re.sub(r'(?<=[\{,\n])\s*(\w+)\s*:', r' "\1":', s)

    # Balance braces — append missing closing braces
    open_count = s.count('{') - s.count('}')
    if open_count > 0:
        s += '}' * open_count
    open_count = s.count('[') - s.count(']')
    if open_count > 0:
        s += ']' * open_count

    return s.strip()


def _try_parse_json(raw: str) -> Any | None:
    """Try parsing JSON with fallback to cleaned JSON."""
    # First try raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try cleaning it up
    cleaned = _clean_json_string(raw)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    return None


# ── Multiple tool call extraction ─────────────────────────────────────────

def extract_all_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract ALL tool calls from a response.

    Returns ``(text_without_tools, [tool_dicts])``.
    """
    start_tag = "<tool_call>"
    end_tag = "</tool_call>"

    tools: list[dict[str, Any]] = []
    clean_parts: list[str] = []
    remaining = text

    while True:
        idx = remaining.find(start_tag)
        if idx == -1:
            clean_parts.append(remaining)
            break

        clean_parts.append(remaining[:idx])
        end_idx = remaining.find(end_tag, idx)
        if end_idx == -1:
            # No closing tag — try to parse everything after start_tag
            json_str = remaining[idx + len(start_tag):].strip()
            parsed = _try_parse_json(json_str)
            if isinstance(parsed, dict):
                tc = _normalize_tool_call(parsed)
                if tc is not None:
                    tools.append(tc)
            clean_parts.append(remaining[idx:])
            break

        json_str = remaining[idx + len(start_tag):end_idx].strip()
        parsed = _try_parse_json(json_str)
        if isinstance(parsed, dict):
            tc = _normalize_tool_call(parsed)
            if tc is not None:
                tools.append(tc)

        remaining = remaining[end_idx + len(end_tag):]

    clean_text = "".join(clean_parts).strip()
    return clean_text, tools


def _normalize_tool_call(obj: Any) -> dict[str, Any] | None:
    """Normalize any tool call format to internal ``{tool, args}`` shape.

    Supports:
    - ``{"tool": "name", "args": {...}}`` (internal)
    - ``{"name": "name", "arguments": {...}}`` (OpenAI)
    - ``{"function": {"name": "name", "arguments": {...}}}`` (Anthropic)
    - ``{"tool_name": "name", "parameters": {...}}`` (misc)
    - ``{"name": "batch_edit", "arguments": [...]}`` (list args → auto-wrap)
    """
    if not isinstance(obj, dict):
        return None

    def _coerce_args(tool_name: str, args: Any) -> dict[str, Any] | None:
        """Coerce args to dict, handling common model mistakes."""
        if isinstance(args, str):
            args = _try_parse_json(args) or {}
        if isinstance(args, dict):
            return args
        # Model sometimes passes a list directly for batch_edit
        if isinstance(args, list) and tool_name in ("batch_edit",):
            return {"edits": args}
        # For edit_file, model sometimes passes [old_string, new_string]
        if isinstance(args, list):
            return None
        return None

    # Already normalized
    if "tool" in obj and isinstance(obj.get("tool"), str):
        tool_name = obj["tool"]
        args = obj.get("args", obj.get("arguments", obj.get("parameters", {})))
        result = _coerce_args(tool_name, args)
        if result is not None:
            return {"tool": tool_name, "args": result}

    # OpenAI-style: {"name": "tool", "arguments": {...}}
    if "name" in obj and isinstance(obj.get("name"), str):
        tool_name = obj["name"]
        args = obj.get("arguments", obj.get("args", obj.get("parameters", {})))
        result = _coerce_args(tool_name, args)
        if result is not None:
            return {"tool": tool_name, "args": result}

    # Anthropic/nested: {"function": {"name": "tool", "arguments": {...}}}
    func = obj.get("function")
    if isinstance(func, dict) and "name" in func:
        tool_name = func["name"]
        args = func.get("arguments", func.get("args", func.get("parameters", {})))
        result = _coerce_args(tool_name, args)
        if result is not None:
            return {"tool": tool_name, "args": result}

    # Misc: {"tool_name": "...", "parameters": {...}}
    if "tool_name" in obj and isinstance(obj.get("tool_name"), str):
        tool_name = obj["tool_name"]
        args = obj.get("parameters", obj.get("args", obj.get("arguments", {})))
        result = _coerce_args(tool_name, args)
        if result is not None:
            return {"tool": tool_name, "args": result}

    return None


def extract_json_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract JSON-style tool calls from model output.

    Supports plain-text function-call objects often emitted by local models,
    for example::

        {"name": "verify_changes", "arguments": {"command": "mypy ."}}

    Returns ``(clean_text, [tool_dicts])`` where each tool dict matches the
    internal shape ``{"tool": name, "args": {...}}``.
    """

    tools: list[dict[str, Any]] = []
    clean_text = text

    # First, inspect fenced JSON blocks.
    block_pattern = _re.compile(r"```(?:json)?\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```", _re.IGNORECASE)
    for match in block_pattern.finditer(text):
        candidate = match.group(1).strip()
        parsed = _try_parse_json(candidate)
        if parsed is None:
            continue

        if isinstance(parsed, list):
            for item in parsed:
                tc = _normalize_tool_call(item)
                if tc is not None:
                    tools.append(tc)
        else:
            tc = _normalize_tool_call(parsed)
            if tc is not None:
                tools.append(tc)

        clean_text = clean_text.replace(match.group(0), "").strip()

    # If nothing found, try parsing the whole response as one JSON object/list.
    if not tools:
        candidate = text.strip()
        if candidate.startswith("{") or candidate.startswith("["):
            parsed = _try_parse_json(candidate)
            if parsed is not None:
                if isinstance(parsed, list):
                    for item in parsed:
                        tc = _normalize_tool_call(item)
                        if tc is not None:
                            tools.append(tc)
                else:
                    tc = _normalize_tool_call(parsed)
                    if tc is not None:
                        tools.append(tc)
                if tools:
                    clean_text = ""

    # Last resort: find balanced JSON objects in the text using brace matching
    if not tools:
        for json_str in _extract_balanced_json(text):
            parsed = _try_parse_json(json_str)
            if parsed is not None:
                tc = _normalize_tool_call(parsed)
                if tc is not None:
                    tools.append(tc)
                    clean_text = clean_text.replace(json_str, "").strip()

    return clean_text, tools


def _extract_balanced_json(text: str) -> list[str]:
    """Extract balanced JSON objects from text using brace counting.

    Finds top-level { ... } blocks that contain tool-call keys and
    returns them as strings for JSON parsing.
    """
    results: list[str] = []
    _tool_keys = ('"tool"', '"name"', '"function"', '"tool_name"')

    i = 0
    while i < len(text):
        if text[i] == '{':
            # Try to find the matching closing brace
            depth = 0
            in_string = False
            escape_next = False
            start = i

            for j in range(i, len(text)):
                ch = text[j]
                if escape_next:
                    escape_next = False
                    continue
                if ch == '\\' and in_string:
                    escape_next = True
                    continue
                if ch == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:j + 1]
                        # Only consider if it looks like a tool call
                        if any(key in candidate for key in _tool_keys):
                            results.append(candidate)
                        i = j + 1
                        break
            else:
                # Unbalanced — skip this opening brace
                i += 1
        else:
            i += 1

    return results


# ── Tool call validation ──────────────────────────────────────────────────

# Required args for each tool
_TOOL_REQUIRED_ARGS: dict[str, list[str]] = {
    "read_file": ["path"],
    "write_file": ["path", "content"],
    "edit_file": ["path", "old_string", "new_string"],
    "edit_lines": ["path", "start_line", "end_line", "new_content"],
    "apply_diff": ["path", "diff"],
    "list_directory": [],
    "run_command": ["command"],
    "search_code": ["pattern"],
    "find_symbols": ["name"],
    "get_project_overview": [],
    "grep_codebase": ["pattern"],
    "verify_changes": [],
    "batch_edit": ["edits"],
}


def validate_tool_call(tool_call: dict[str, Any]) -> str | None:
    """Validate a tool call dict. Returns error message or None if valid."""
    tool_name = tool_call.get("tool", "")
    args = tool_call.get("args", {})

    if tool_name not in _TOOL_REQUIRED_ARGS:
        from difflib import get_close_matches
        valid = list(_TOOL_REQUIRED_ARGS.keys())
        close = get_close_matches(tool_name, valid, n=2, cutoff=0.5)
        suggestion = f" Did you mean: {', '.join(close)}?" if close else ""
        return f"Unknown tool '{tool_name}'.{suggestion} Valid tools: {', '.join(valid)}"

    required = _TOOL_REQUIRED_ARGS[tool_name]
    missing = [arg for arg in required if arg not in args]
    if missing:
        return (
            f"Tool '{tool_name}' missing required args: {', '.join(missing)}. "
            f"Required: {', '.join(required)}"
        )

    return None


# ── Tool call hashing for loop detection ──────────────────────────────────

def hash_tool_call(tool_name: str, args: dict[str, Any]) -> str:
    """Create a deterministic hash of a tool call for deduplication."""
    key = json.dumps({"tool": tool_name, "args": args}, sort_keys=True, default=str)
    return hashlib.md5(key.encode()).hexdigest()[:12]


# ── Tool executor ────────────────────────────────────────────────────────

class ToolExecutor:
    """Executes tool calls requested by the LLM within a repo sandbox."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path.resolve()

    # -- public API --------------------------------------------------------

    def extract_tool_call(self, text: str) -> tuple[str, dict[str, Any] | None]:
        """Extract a tool call from the LLM response text.

        Returns ``(text_before_tool, tool_dict)`` or ``(full_text, None)``
        if no tool call is found.
        """
        start_tag = "<tool_call>"
        end_tag = "</tool_call>"

        idx = text.find(start_tag)
        if idx == -1:
            return text, None

        end_idx = text.find(end_tag, idx)
        if end_idx == -1:
            return text, None

        before = text[:idx]
        json_str = text[idx + len(start_tag):end_idx].strip()

        try:
            tool_data = json.loads(json_str)
            if isinstance(tool_data, dict) and "tool" in tool_data:
                return before, tool_data
        except json.JSONDecodeError:
            pass

        return text, None

    def execute(self, tool_name: str, args: dict[str, Any]) -> str:
        """Dispatch and run a tool, returning the result as a string."""
        dispatch = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "edit_lines": self._edit_lines,
            "apply_diff": self._apply_diff,
            "list_directory": self._list_directory,
            "run_command": self._run_command,
            "search_code": self._search_code,
            "find_symbols": self._find_symbols,
            "get_project_overview": self._get_project_overview,
            "grep_codebase": self._grep_codebase,
            "verify_changes": self._verify_changes,
            "batch_edit": self._batch_edit,
        }

        handler = dispatch.get(tool_name)
        if handler is None:
            # Suggest closest match
            from difflib import get_close_matches
            close = get_close_matches(tool_name, list(dispatch.keys()), n=2, cutoff=0.5)
            suggestion = f" Did you mean: {', '.join(close)}?" if close else ""
            return f"Error: Unknown tool '{tool_name}'.{suggestion}"

        try:
            return handler(args)
        except Exception as exc:
            return f"Error: {exc}"

    # -- tool implementations ----------------------------------------------

    def _resolve_path(self, rel_path: str) -> Path:
        """Resolve a relative path safely within the repo.

        If the exact path doesn't exist, tries stripping common prefixes
        (src/, lib/, app/) that models often hallucinate.
        """
        target = (self.repo_path / rel_path).resolve()
        if not target.is_relative_to(self.repo_path):
            raise ValueError(f"Path traversal blocked: {rel_path!r}")

        # If file exists, return it directly
        if target.exists():
            return target

        # Try stripping common hallucinated prefixes
        stripped = rel_path
        _COMMON_PREFIXES = ("src/", "lib/", "app/", "source/", "code/", "./")
        for prefix in _COMMON_PREFIXES:
            if stripped.lower().startswith(prefix):
                stripped = stripped[len(prefix):]
                candidate = (self.repo_path / stripped).resolve()
                if candidate.is_relative_to(self.repo_path) and candidate.exists():
                    return candidate

        # Try just the filename
        filename = Path(rel_path).name
        candidates = list(self.repo_path.rglob(filename))
        if len(candidates) == 1 and candidates[0].is_relative_to(self.repo_path):
            return candidates[0]

        return target  # Return original (will fail with "not found")

    def _read_file(self, args: dict[str, Any]) -> str:
        path = self._resolve_path(args.get("path", ""))
        if not path.is_file():
            suggestion = self._suggest_path(args.get("path", ""))
            return f"Error: File not found: {args.get('path')}{suggestion}"

        text = path.read_text(encoding="utf-8", errors="replace")
        # Strip UTF-8 BOM so the model never sees it
        if text.startswith("\ufeff"):
            text = text[1:]
        lines = text.splitlines(keepends=True)

        start = args.get("start_line")
        end = args.get("end_line")
        if start is not None or end is not None:
            s = max(0, (int(start) - 1)) if start else 0
            e = int(end) if end else len(lines)
            lines = lines[s:e]

        content = "".join(lines)
        if len(content) > _MAX_OUTPUT:
            content = content[:_MAX_OUTPUT] + "\n... (truncated)"
        return content

    def _write_file(self, args: dict[str, Any]) -> str:
        rel = args.get("path", "")
        path = self._resolve_path(rel)
        content = args.get("content", "")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} bytes to {rel}"

    def _edit_file(self, args: dict[str, Any]) -> str:
        rel = args.get("path", "")
        path = self._resolve_path(rel)
        if not path.is_file():
            suggestion = self._suggest_path(rel)
            return f"Error: File not found: {rel}{suggestion}"

        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        if not old_string:
            return "Error: old_string is required"

        # Phase 1: No-op detection — reject edits where old == new
        if old_string == new_string:
            return "Error: old_string and new_string are identical (no-op edit). Skipping."

        text = path.read_text(encoding="utf-8")

        # Strip UTF-8 BOM if present — models never include it in old_string
        if text.startswith("\ufeff"):
            text = text[1:]
            # Rewrite without BOM immediately so future reads are clean
            path.write_text(text, encoding="utf-8")

        # Phase 3: Try exact match first
        count = text.count(old_string)
        if count == 1:
            new_text = text.replace(old_string, new_string, 1)
            # Phase 7: Syntax validation for Python files
            syntax_err = self._validate_syntax_if_python(path, new_text)
            if syntax_err:
                return f"Error: Edit would create a syntax error: {syntax_err}. Edit reverted."
            path.write_text(new_text, encoding="utf-8")
            return f"Successfully edited {rel}"

        if count > 1:
            # Auto-provide context so model doesn't need another round trip
            context_info = self._get_match_context(text, old_string, max_matches=3)
            return (
                f"Error: old_string matches {count} locations — be more specific. "
                f"Include more surrounding lines to disambiguate.\n{context_info}"
            )

        # Phase 3: Exact match failed — try whitespace-normalized match
        normalized_text = self._normalize_whitespace(text)
        normalized_old = self._normalize_whitespace(old_string)
        if normalized_old and normalized_text.count(normalized_old) == 1:
            # Find actual position in original text via line-by-line matching
            result = self._apply_normalized_edit(text, old_string, new_string)
            if result is not None:
                syntax_err = self._validate_syntax_if_python(path, result)
                if syntax_err:
                    return f"Error: Edit would create a syntax error: {syntax_err}. Edit reverted."
                path.write_text(result, encoding="utf-8")
                return f"Successfully edited {rel} (whitespace-normalized match)"

        # Phase 3: Try fuzzy matching as last resort
        match_result = self._fuzzy_find(text, old_string)
        if match_result is not None:
            start, end, ratio = match_result
            new_text = text[:start] + new_string + text[end:]
            syntax_err = self._validate_syntax_if_python(path, new_text)
            if syntax_err:
                return f"Error: Edit would create a syntax error: {syntax_err}. Edit reverted."
            path.write_text(new_text, encoding="utf-8")
            return f"Successfully edited {rel} (fuzzy match, {ratio:.0%} confidence)"

        # All match strategies failed — provide helpful context
        context = self._get_nearby_context(text, old_string)
        return (
            f"Error: old_string not found in file (exact, normalized, and fuzzy match all failed). "
            f"Read the file first to get exact content.\n{context}"
        )

    def _fuzzy_find(
        self, text: str, search: str,
    ) -> tuple[int, int, float] | None:
        """Find the best fuzzy match for *search* in *text*.

        Returns ``(start, end, ratio)`` or ``None`` if no match above threshold.
        Uses line-level difflib matching for efficiency.
        """
        search_lines = search.splitlines(keepends=True)
        text_lines = text.splitlines(keepends=True)

        if not search_lines or not text_lines:
            return None

        search_len = len(search_lines)
        best_ratio = 0.0
        best_start_line = 0

        # Sliding window over text lines
        window_min = max(1, search_len - max(3, search_len // 5))
        window_max = search_len + max(3, search_len // 5)

        for win_size in range(search_len, window_max + 1):
            for i in range(len(text_lines) - win_size + 1):
                candidate = text_lines[i:i + win_size]
                ratio = difflib.SequenceMatcher(
                    None, search_lines, candidate,
                ).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_start_line = i
                    best_win_size = win_size
                    if ratio > 0.98:  # Close enough, stop early
                        break
            if best_ratio > 0.98:
                break

        # Also try smaller windows
        for win_size in range(window_min, search_len):
            for i in range(len(text_lines) - win_size + 1):
                candidate = text_lines[i:i + win_size]
                ratio = difflib.SequenceMatcher(
                    None, search_lines, candidate,
                ).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_start_line = i
                    best_win_size = win_size

        if best_ratio < _FUZZY_MATCH_THRESHOLD:
            return None

        # Convert line range to character offsets
        start = sum(len(l) for l in text_lines[:best_start_line])
        end = sum(len(l) for l in text_lines[:best_start_line + best_win_size])
        return start, end, best_ratio

    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        """Normalize whitespace for matching: tabs→spaces, strip trailing."""
        lines = text.splitlines()
        return "\n".join(line.expandtabs(4).rstrip() for line in lines)

    def _apply_normalized_edit(
        self, text: str, old_string: str, new_string: str,
    ) -> str | None:
        """Apply edit using whitespace-normalized matching."""
        norm_text_lines = self._normalize_whitespace(text).splitlines()
        norm_old_lines = self._normalize_whitespace(old_string).splitlines()

        if not norm_old_lines:
            return None

        # Find where normalized old_string starts in normalized text
        target = "\n".join(norm_old_lines)
        full_norm = "\n".join(norm_text_lines)

        idx = full_norm.find(target)
        if idx == -1:
            return None

        # Find the line number range
        start_line = full_norm[:idx].count("\n")
        end_line = start_line + len(norm_old_lines)

        # Replace in original text
        original_lines = text.splitlines(keepends=True)
        before = original_lines[:start_line]
        after = original_lines[end_line:]

        # Ensure new_string ends with newline if replacing whole lines
        replacement = new_string
        if not replacement.endswith("\n") and after:
            replacement += "\n"

        return "".join(before) + replacement + "".join(after)

    @staticmethod
    def _validate_syntax_if_python(path: Path, content: str) -> str | None:
        """If path is a Python file, check syntax. Returns error string or None."""
        if path.suffix != ".py":
            return None
        try:
            ast.parse(content, filename=str(path))
            return None
        except SyntaxError as e:
            hint = ""
            msg = e.msg or ""
            if "expected an indented block" in msg:
                hint = (
                    " Hint: you removed the only statement in a block (for/if/def/class). "
                    "Include the ENTIRE block (for-loop, function, etc.) in old_string "
                    "and rewrite it completely in new_string, or use `pass` as a placeholder."
                )
            elif "unexpected indent" in msg:
                hint = " Hint: check indentation in new_string matches the surrounding code."
            return f"line {e.lineno}: {msg}.{hint}"

    def _suggest_path(self, rel_path: str) -> str:
        """Suggest similar file paths when a file is not found."""
        if not rel_path:
            return ""
        target_name = Path(rel_path).name.lower()
        candidates: list[str] = []
        _skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".localforge"}
        for dirpath, dirnames, filenames in os.walk(self.repo_path):
            dirnames[:] = [d for d in dirnames if d not in _skip and not d.startswith(".")]
            for fname in filenames:
                if difflib.SequenceMatcher(None, target_name, fname.lower()).ratio() > 0.6:
                    try:
                        fp = Path(dirpath) / fname
                        candidates.append(str(fp.relative_to(self.repo_path)))
                    except ValueError:
                        pass
                if len(candidates) >= 5:
                    break
            if len(candidates) >= 5:
                break
        if candidates:
            return "\nDid you mean: " + ", ".join(candidates[:3]) + "?"
        return ""

    def _get_match_context(self, text: str, pattern: str, max_matches: int = 3) -> str:
        """Return context lines around each match location for disambiguation."""
        lines = text.splitlines()
        positions: list[int] = []
        start = 0
        while True:
            idx = text.find(pattern, start)
            if idx == -1 or len(positions) >= max_matches:
                break
            positions.append(idx)
            start = idx + 1

        if not positions:
            return ""

        parts = ["Matches found at:"]
        for pos in positions:
            line_num = text[:pos].count("\n") + 1
            start_l = max(0, line_num - 3)
            end_l = min(len(lines), line_num + 3)
            context_lines = lines[start_l:end_l]
            numbered = [f"  L{start_l + i + 1}: {l}" for i, l in enumerate(context_lines)]
            parts.append(f"\n--- Match at line {line_num} ---")
            parts.extend(numbered)

        return "\n".join(parts)

    def _get_nearby_context(self, text: str, search: str) -> str:
        """When edit fails completely, show relevant portions of the file."""
        lines = text.splitlines()
        total = len(lines)
        if total == 0:
            return "File is empty."

        # Try to find the most similar region in the file
        search_lines = search.splitlines()
        if not search_lines:
            return f"File has {total} lines."

        first_search_line = search_lines[0].strip()
        best_line = 0
        best_score = 0.0
        for i, line in enumerate(lines):
            score = difflib.SequenceMatcher(None, first_search_line, line.strip()).ratio()
            if score > best_score:
                best_score = score
                best_line = i

        if best_score < 0.3:
            # Show first/last lines of file as reference
            preview_lines = min(_EDIT_ERROR_CONTEXT_LINES, total)
            sample = lines[:preview_lines]
            numbered = [f"  L{i + 1}: {l}" for i, l in enumerate(sample)]
            return f"File has {total} lines. First {preview_lines} lines:\n" + "\n".join(numbered)

        # Show context around best match
        start = max(0, best_line - 5)
        end = min(total, best_line + _EDIT_ERROR_CONTEXT_LINES)
        sample = lines[start:end]
        numbered = [f"  L{start + i + 1}: {l}" for i, l in enumerate(sample)]
        return (
            f"Closest matching region (L{start + 1}-{end}, {best_score:.0%} similar to first line):\n"
            + "\n".join(numbered)
        )

    def _edit_lines(self, args: dict[str, Any]) -> str:
        """Replace a range of lines by line number."""
        rel = args.get("path", "")
        path = self._resolve_path(rel)
        if not path.is_file():
            suggestion = self._suggest_path(rel)
            return f"Error: File not found: {rel}{suggestion}"

        start_line = int(args.get("start_line", 0))
        end_line = int(args.get("end_line", 0))
        new_content = args.get("new_content", "")

        if start_line < 1 or end_line < start_line:
            return f"Error: Invalid line range ({start_line}-{end_line}). Lines are 1-based inclusive."

        text = path.read_text(encoding="utf-8")
        # Strip UTF-8 BOM
        if text.startswith("\ufeff"):
            text = text[1:]
        lines = text.splitlines(keepends=True)
        total = len(lines)

        if start_line > total:
            return f"Error: start_line {start_line} exceeds file length ({total} lines)"

        # Clamp end_line to file length
        end_line = min(end_line, total)

        before = lines[:start_line - 1]
        after = lines[end_line:]

        # Ensure new content ends with newline if there's content after
        replacement = new_content
        if replacement and not replacement.endswith("\n") and after:
            replacement += "\n"

        new_text = "".join(before) + replacement + "".join(after)

        # Syntax validation
        syntax_err = self._validate_syntax_if_python(path, new_text)
        if syntax_err:
            return f"Error: Edit would create a syntax error: {syntax_err}. Edit reverted."

        path.write_text(new_text, encoding="utf-8")
        return f"Successfully replaced lines {start_line}-{end_line} in {rel}"

    def _apply_diff(self, args: dict[str, Any]) -> str:
        """Apply a unified diff to a file."""
        rel = args.get("path", "")
        path = self._resolve_path(rel)
        if not path.is_file():
            suggestion = self._suggest_path(rel)
            return f"Error: File not found: {rel}{suggestion}"

        diff_text = args.get("diff", "")
        if not diff_text:
            return "Error: diff content is required"

        text = path.read_text(encoding="utf-8")
        original_lines = text.splitlines(keepends=True)
        # Ensure all lines have line endings for consistent matching
        if original_lines and not original_lines[-1].endswith("\n"):
            original_lines[-1] += "\n"

        try:
            patched_lines = self._apply_unified_diff(original_lines, diff_text)
        except ValueError as e:
            return f"Error: Failed to apply diff: {e}"

        new_text = "".join(patched_lines)
        syntax_err = self._validate_syntax_if_python(path, new_text)
        if syntax_err:
            return f"Error: Diff would create a syntax error: {syntax_err}. Diff not applied."

        path.write_text(new_text, encoding="utf-8")
        return f"Successfully applied diff to {rel}"

    @staticmethod
    def _apply_unified_diff(
        original_lines: list[str], diff_text: str,
    ) -> list[str]:
        """Parse and apply a unified diff to a list of lines.

        This is a simple unified-diff applier that handles basic hunks.
        """
        result = list(original_lines)
        offset = 0  # Track line number shifts from previous hunks

        hunk_header = _re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')
        diff_lines = diff_text.splitlines(keepends=True)

        i = 0
        while i < len(diff_lines):
            line = diff_lines[i]
            match = hunk_header.match(line.rstrip())
            if match:
                orig_start = int(match.group(1)) - 1 + offset
                # Collect hunk lines
                i += 1
                removals = 0
                additions: list[str] = []
                hunk_pos = orig_start

                while i < len(diff_lines):
                    hl = diff_lines[i]
                    if hl.startswith("@@") or hl.startswith("---") or hl.startswith("+++"):
                        break
                    if hl.startswith("-"):
                        removals += 1
                        i += 1
                    elif hl.startswith("+"):
                        add_content = hl[1:]
                        if not add_content.endswith("\n"):
                            add_content += "\n"
                        additions.append(add_content)
                        i += 1
                    elif hl.startswith(" ") or hl.startswith("\n") or hl.strip() == "":
                        # Context line — flush pending changes at current position
                        if removals or additions:
                            # Apply the pending change
                            result[hunk_pos:hunk_pos + removals] = additions
                            offset += len(additions) - removals
                            hunk_pos += len(additions)
                            removals = 0
                            additions = []
                        else:
                            hunk_pos += 1
                        i += 1
                    elif hl.startswith("\\"):
                        i += 1  # "\ No newline at end of file"
                    else:
                        break

                # Flush any remaining pending changes
                if removals or additions:
                    result[hunk_pos:hunk_pos + removals] = additions
                    offset += len(additions) - removals
            else:
                i += 1

        return result

    def _list_directory(self, args: dict[str, Any]) -> str:
        rel = args.get("path", ".")
        path = self._resolve_path(rel)
        if not path.is_dir():
            return f"Error: Directory not found: {rel}"

        entries: list[str] = []
        for child in sorted(path.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                entries.append(f"  {child.name}/")
            else:
                try:
                    size = child.stat().st_size
                    if size < 1024:
                        size_str = f"{size}B"
                    elif size < 1024 * 1024:
                        size_str = f"{size // 1024}KB"
                    else:
                        size_str = f"{size // (1024 * 1024)}MB"
                    entries.append(f"  {child.name}  ({size_str})")
                except OSError:
                    entries.append(f"  {child.name}")

        if not entries:
            return "(empty directory)"
        return "\n".join(entries)

    def _run_command(self, args: dict[str, Any]) -> str:
        command = args.get("command", "").strip()
        if not command:
            return "Error: No command provided"

        command = self._normalize_command(command)

        timeout = int(args.get("timeout", 120))
        # Clamp timeout to a safe range
        timeout = max(5, min(timeout, 600))

        # Safety check: block destructive commands
        first_token = command.split()[0].lower() if command.split() else ""
        # Strip path prefixes (e.g., /usr/bin/rm → rm)
        first_token = first_token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if first_token in _BLOCKED_COMMANDS:
            return f"Error: Command '{first_token}' is blocked for safety"

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.repo_path),
                env=None,  # inherit parent env
            )
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {timeout} seconds"

        output_parts: list[str] = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"STDERR:\n{result.stderr}")
        if result.returncode != 0:
            output_parts.append(f"(exit code: {result.returncode})")

        output = "\n".join(output_parts) if output_parts else "(no output)"

        # Auto-retry: if a Python tool isn't in PATH, try `python -m <tool>`
        if result.returncode != 0 and self._is_tool_not_found(output, command):
            python_m_cmd = self._try_python_m_fallback(command)
            if python_m_cmd:
                try:
                    retry = subprocess.run(
                        python_m_cmd,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        cwd=str(self.repo_path),
                        env=None,
                    )
                    retry_parts: list[str] = [
                        f"(auto-corrected: {python_m_cmd})",
                    ]
                    if retry.stdout:
                        retry_parts.append(retry.stdout)
                    if retry.stderr:
                        retry_parts.append(f"STDERR:\n{retry.stderr}")
                    if retry.returncode != 0:
                        retry_parts.append(f"(exit code: {retry.returncode})")
                    return "\n".join(retry_parts) if retry_parts else "(no output)"
                except subprocess.TimeoutExpired:
                    pass  # Fall through to original output

        # Retry once with corrected Ruff syntax for common model mistakes.
        if result.returncode != 0 and self._looks_like_ruff_usage_error(output):
            corrected = self._normalize_ruff_command(command)
            if corrected != command:
                try:
                    retry = subprocess.run(
                        corrected,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        cwd=str(self.repo_path),
                        env=None,
                    )
                    retry_parts: list[str] = [
                        f"Auto-corrected command: {corrected}",
                    ]
                    if retry.stdout:
                        retry_parts.append(retry.stdout)
                    if retry.stderr:
                        retry_parts.append(f"STDERR:\n{retry.stderr}")
                    if retry.returncode != 0:
                        retry_parts.append(f"(exit code: {retry.returncode})")
                    output = "\n".join(retry_parts)
                except subprocess.TimeoutExpired:
                    output = (
                        output
                        + "\nAuto-corrected Ruff command timed out."
                    )

        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + "\n... (truncated)"

        # Phase 6: Compress large linter/test output to save tokens
        output = self._compress_tool_output(output, command)
        return output

    @staticmethod
    def _compress_tool_output(output: str, command: str) -> str:
        """Compress large tool outputs by summarizing repetitive errors.

        Groups errors by type for linter outputs, keeping only first few
        examples of each error type to save context tokens.
        """
        lines = output.splitlines()
        if len(lines) < 30:
            return output  # Not big enough to compress

        lower_cmd = command.lower()

        # Detect linter output (ruff, flake8, pylint, mypy)
        is_linter = any(kw in lower_cmd for kw in ("ruff", "flake8", "pylint", "mypy"))
        if not is_linter:
            return output

        # Group errors by error code/type
        error_groups: dict[str, list[str]] = {}
        other_lines: list[str] = []
        error_pattern = _re.compile(r'(\w+\.\w+:\d+:\d+:\s*)(\w+\d*)\s')

        for line in lines:
            m = error_pattern.search(line)
            if m:
                code = m.group(2)
                error_groups.setdefault(code, []).append(line)
            else:
                other_lines.append(line)

        if not error_groups or len(error_groups) < 2:
            return output  # Not grouped enough to compress

        # Build compressed output
        compressed: list[str] = []
        total_errors = sum(len(v) for v in error_groups.values())
        compressed.append(f"=== {total_errors} issues grouped by type ===\n")

        for code, group_lines in sorted(error_groups.items(), key=lambda x: -len(x[1])):
            compressed.append(f"\n[{code}] — {len(group_lines)} occurrence(s):")
            # Show first 3 examples
            for line in group_lines[:3]:
                compressed.append(f"  {line}")
            if len(group_lines) > 3:
                compressed.append(f"  ... and {len(group_lines) - 3} more")

        # Add summary lines (exit code, etc.)
        for line in other_lines[-5:]:
            if line.strip():
                compressed.append(line)

        result = "\n".join(compressed)
        # Only use compressed if it's actually shorter
        if len(result) < len(output) * 0.8:
            return result
        return output

    # Python tools that can be invoked via `python -m <tool>` when not in PATH
    _PYTHON_M_TOOLS = {
        "ruff", "pytest", "mypy", "black", "isort", "flake8", "pylint",
        "coverage", "pip", "poetry", "pdm", "hatch", "nox", "tox",
        "pre_commit", "pre-commit", "autopep8", "pyflakes", "pycodestyle",
        "bandit", "pydocstyle", "vulture",
    }

    @staticmethod
    def _is_tool_not_found(output: str, command: str) -> bool:
        """Detect if a command failed because the executable wasn't found."""
        lower = output.lower()
        not_found_signals = (
            "is not recognized",  # Windows
            "not found",          # Linux/macOS
            "no such file or directory",
            "command not found",
            "not operable",
        )
        return any(sig in lower for sig in not_found_signals)

    @classmethod
    def _try_python_m_fallback(cls, command: str) -> str | None:
        """Try converting a command to `python -m <tool>` form.

        Returns the corrected command or None if not applicable.
        """
        parts = command.strip().split()
        if not parts:
            return None

        tool = parts[0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        # Strip .exe suffix on Windows
        if tool.endswith(".exe"):
            tool = tool[:-4]

        if tool in cls._PYTHON_M_TOOLS:
            rest = parts[1:]
            return f"python -m {tool} {' '.join(rest)}".strip()

        return None

    @staticmethod
    def _looks_like_ruff_usage_error(output: str) -> bool:
        lower = output.lower()
        return (
            "usage: ruff" in lower
            and (
                "unexpected argument '--fix'" in lower
                or "unrecognized subcommand '.'" in lower
            )
        )

    @staticmethod
    def _normalize_ruff_command(command: str) -> str:
        """Normalize common Ruff invocation mistakes to modern CLI syntax."""
        stripped = command.strip()
        if not stripped.lower().startswith("ruff"):
            return command

        try:
            parts = shlex.split(stripped, posix=False)
        except ValueError:
            # Fall back to simple split when quoting is malformed
            parts = stripped.split()

        if len(parts) <= 1:
            return "ruff check ."

        # Drop leading executable token
        args = parts[1:]
        lower_args = [a.lower() for a in args]

        # Already modern form
        if args and args[0].lower() in {"check", "format", "rule", "config", "clean", "server", "analyze"}:
            return command

        fix_flag = "--fix" in lower_args
        other_flags = [a for a in args if a.lower() != "--fix"]

        # Case: "ruff ." -> "ruff check ."
        if other_flags == ["."] and not fix_flag:
            return "ruff check ."

        # Case: "ruff --fix ." or "ruff . --fix" -> "ruff check . --fix"
        if other_flags == ["."] and fix_flag:
            return "ruff check . --fix"

        # Generic fallback: preserve user args under "check"
        rebuilt = ["ruff", "check", *other_flags]
        if fix_flag:
            rebuilt.append("--fix")
        return " ".join(rebuilt)

    def _normalize_command(self, command: str) -> str:
        """Normalize known command pitfalls before execution.

        - Normalizes ruff CLI syntax
        - On Windows, preemptively uses `python -m <tool>` for Python tools
          that aren't in PATH (avoids "not recognized" errors)
        """
        command = self._normalize_ruff_command(command)

        # On Windows, proactively check if the tool is in PATH
        if sys.platform == "win32":
            import shutil
            parts = command.strip().split()
            if parts:
                tool = parts[0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                if tool.endswith(".exe"):
                    tool = tool[:-4]
                # If it's a known Python tool and not in PATH, use python -m
                if tool in self._PYTHON_M_TOOLS and not shutil.which(tool):
                    rest = parts[1:]
                    command = f"python -m {tool} {' '.join(rest)}".strip()

        return command

    def _search_code(self, args: dict[str, Any]) -> str:
        pattern = args.get("pattern", "")
        if not pattern:
            return "Error: No search pattern provided"

        file_glob = args.get("file_glob", "")

        # Use the index searcher if available, otherwise fall back to grep
        try:
            from localforge.index import IndexSearcher

            db_path = self.repo_path / ".localforge" / "index.db"
            if db_path.is_file():
                searcher = IndexSearcher(db_path)
                try:
                    results = searcher.search_lexical(pattern, limit=15)
                    if results:
                        parts: list[str] = []
                        for r in results:
                            parts.append(
                                f"{r.file_path}:"
                                f"{r.start_line} — "
                                f"{r.content[:120]}"
                            )
                        return "\n".join(parts)
                finally:
                    searcher.close()
        except Exception:
            pass

        # Fallback: simple grep via subprocess
        if sys.platform == "win32":
            cmd = ["findstr", "/S", "/I", "/N", pattern]
            if file_glob:
                cmd.append(file_glob)
            else:
                cmd.append("*.py")
        else:
            cmd = ["grep", "-r", "-n", "-i", "--include", file_glob or "*.py", pattern, "."]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                cwd=str(self.repo_path),
            )
            output = result.stdout or "(no matches)"
            if len(output) > _MAX_OUTPUT:
                output = output[:_MAX_OUTPUT] + "\n... (truncated)"
            return output
        except subprocess.TimeoutExpired:
            return "Error: Search timed out"

    def _find_symbols(self, args: dict[str, Any]) -> str:
        name = args.get("name", "")
        kind = args.get("kind")
        if not name:
            return "Error: No symbol name provided"

        try:
            from localforge.index import IndexSearcher

            db_path = self.repo_path / ".localforge" / "index.db"
            if not db_path.is_file():
                return "Error: No index found. Run 'localforge index' first."

            searcher = IndexSearcher(db_path)
            try:
                results = searcher.search_symbols(name, kind=kind)
                if not results:
                    return f"No symbols found matching '{name}'"
                parts: list[str] = []
                for r in results[:30]:
                    parts.append(
                        f"  {r['kind']:12s} {r['name']:30s} "
                        f"{r['file_path']}:L{r['line']}"
                    )
                return f"Found {len(results)} symbol(s):\n" + "\n".join(parts)
            finally:
                searcher.close()
        except Exception as exc:
            return f"Error searching symbols: {exc}"

    def _get_project_overview(self, args: dict[str, Any]) -> str:
        import os

        _SKIP_DIRS = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".localforge", ".tox", ".mypy_cache",
            ".pytest_cache",
        }

        lines: list[str] = ["PROJECT STRUCTURE:"]

        # Check for README
        readme_content = ""
        for name in ("README.md", "README.rst", "README.txt", "README"):
            readme_path = self.repo_path / name
            if readme_path.is_file():
                try:
                    readme_content = readme_path.read_text(encoding="utf-8", errors="replace")
                    if len(readme_content) > 3000:
                        readme_content = readme_content[:3000] + "\n... (truncated)"
                except OSError:
                    pass
                break

        # Build tree
        file_count = 0
        for dirpath, dirnames, filenames in os.walk(self.repo_path):
            dirnames[:] = [
                d for d in sorted(dirnames)
                if d not in _SKIP_DIRS and not d.startswith(".")
            ]
            try:
                rel = Path(dirpath).relative_to(self.repo_path)
            except ValueError:
                continue
            depth = len(rel.parts)
            if depth > 4:
                continue

            indent = "  " * depth
            dir_name = rel.name if depth > 0 else "."
            lines.append(f"{indent}{dir_name}/")

            for fname in sorted(filenames):
                if fname.startswith(".") or fname.endswith((".pyc", ".pyo")):
                    continue
                file_count += 1
                if file_count > 150:
                    lines.append(f"{indent}  ... (more files)")
                    break
                lines.append(f"{indent}  {fname}")
            if file_count > 150:
                break

        # Add package/config info
        pkg_info = []
        for cfg_file in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml"):
            if (self.repo_path / cfg_file).is_file():
                try:
                    content = (self.repo_path / cfg_file).read_text(encoding="utf-8")
                    if len(content) > 2000:
                        content = content[:2000] + "\n... (truncated)"
                    pkg_info.append(f"\n{cfg_file}:\n{content}")
                except OSError:
                    pass

        # Add symbols from index
        symbols_info = ""
        try:
            from localforge.index import IndexSearcher

            db_path = self.repo_path / ".localforge" / "index.db"
            if db_path.is_file():
                searcher = IndexSearcher(db_path)
                try:
                    conn = searcher._get_conn()
                    rows = conn.execute(
                        """
                        SELECT s.name, s.kind, s.line, f.relative_path
                          FROM symbols s
                          JOIN files f ON f.id = s.file_id
                         WHERE s.kind IN ('class', 'function', 'interface')
                           AND (s.scope = 'module' OR s.kind = 'class')
                         ORDER BY f.relative_path, s.line
                         LIMIT 100
                        """
                    ).fetchall()

                    if rows:
                        sym_lines = ["\nKEY DEFINITIONS:"]
                        by_file: dict[str, list[str]] = {}
                        for row in rows:
                            fp = row["relative_path"]
                            entry = f"    {row['kind']}: {row['name']} (L{row['line']})"
                            by_file.setdefault(fp, []).append(entry)
                        for fp, entries in sorted(by_file.items()):
                            sym_lines.append(f"  {fp}")
                            sym_lines.extend(entries[:8])
                        symbols_info = "\n".join(sym_lines)
                finally:
                    searcher.close()
        except Exception:
            pass

        result = "\n".join(lines)
        if readme_content:
            result += f"\n\nREADME:\n{readme_content}"
        if pkg_info:
            result += "\n" + "\n".join(pkg_info)
        if symbols_info:
            result += symbols_info

        return result

    # -- new tools: grep_codebase, verify_changes, batch_edit ---------------

    def _grep_codebase(self, args: dict[str, Any]) -> str:
        """Recursive grep across the entire codebase."""
        pattern = args.get("pattern", "")
        if not pattern:
            return "Error: No search pattern provided"

        file_glob = args.get("file_glob", "")
        is_regex = args.get("is_regex", False)

        _SKIP_DIRS = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".localforge", ".tox", ".mypy_cache",
            ".pytest_cache", ".eggs",
        }

        matches: list[str] = []
        max_matches = 100

        if is_regex:
            try:
                compiled = _re.compile(pattern, _re.IGNORECASE)
            except _re.error as e:
                return f"Error: Invalid regex: {e}"
        else:
            compiled = None

        for dirpath, dirnames, filenames in os.walk(self.repo_path):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.startswith(".")
            ]
            for fname in filenames:
                if fname.startswith(".") or fname.endswith((".pyc", ".pyo", ".exe", ".dll", ".so")):
                    continue
                if file_glob and not Path(fname).match(file_glob):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    # Skip binary files
                    if fpath.stat().st_size > 500_000:
                        continue
                    text = fpath.read_text(encoding="utf-8", errors="replace")
                except (OSError, UnicodeDecodeError):
                    continue

                try:
                    rel = fpath.relative_to(self.repo_path)
                except ValueError:
                    continue

                for i, line in enumerate(text.splitlines(), 1):
                    found = False
                    if compiled:
                        found = bool(compiled.search(line))
                    else:
                        found = pattern.lower() in line.lower()
                    if found:
                        matches.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                        if len(matches) >= max_matches:
                            break
                if len(matches) >= max_matches:
                    break

        if not matches:
            return f"No matches found for '{pattern}'"

        result = f"Found {len(matches)} match(es):\n" + "\n".join(matches)
        if len(matches) >= max_matches:
            result += "\n... (results capped at 100)"
        return result

    def _verify_changes(self, args: dict[str, Any]) -> str:
        """Run project verification (tests, lint, type checks)."""
        custom_command = args.get("command", "").strip()

        if custom_command:
            # Run a specific verification command
            return self._run_command({"command": custom_command, "timeout": 300})

        # Auto-detect and run all verification.
        # We reuse the VerificationRunner when available.
        results_parts: list[str] = []

        try:
            from localforge.core.config import LocalForgeConfig
            from localforge.verifier.runner import VerificationRunner

            config = LocalForgeConfig(repo_path=str(self.repo_path))
            runner = VerificationRunner(self.repo_path, config)
            vresults = runner.run_verification()

            if not vresults:
                return "No verification commands detected for this project."

            all_passed = True
            for vr in vresults:
                status = "PASS" if vr.success else "FAIL"
                if not vr.success:
                    all_passed = False
                results_parts.append(f"[{status}] {vr.command}")
                if vr.stdout:
                    out_text = vr.stdout[:5000]
                    results_parts.append(out_text)
                if vr.stderr and not vr.success:
                    err_text = vr.stderr[:3000]
                    results_parts.append(f"STDERR: {err_text}")

            summary = "ALL CHECKS PASSED" if all_passed else "SOME CHECKS FAILED"
            results_parts.insert(0, f"=== VERIFICATION SUMMARY: {summary} ===\n")
            return "\n".join(results_parts)

        except ImportError:
            # Fallback: try pytest directly
            return self._run_command({"command": "python -m pytest --tb=short -q", "timeout": 300})

    def _batch_edit(self, args: dict[str, Any]) -> str:
        """Apply multiple edits across files."""
        edits = args.get("edits", [])
        if not edits or not isinstance(edits, list):
            return "Error: 'edits' must be a non-empty list of {path, old_string, new_string}"

        results: list[str] = []
        for i, edit in enumerate(edits, 1):
            path = edit.get("path", "")
            old_string = edit.get("old_string", "")
            new_string = edit.get("new_string", "")
            result = self._edit_file({
                "path": path,
                "old_string": old_string,
                "new_string": new_string,
            })
            results.append(f"  Edit {i} ({path}): {result}")

        return "\n".join(results)
