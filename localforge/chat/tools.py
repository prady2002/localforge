"""Tool definitions and executor for the chat engine's tool-use loop.

The LLM can invoke these tools by outputting a JSON block with a ``tool``
key.  The tool executor runs the request, captures the result, and returns
it as a string to be fed back to the model.
"""

from __future__ import annotations

import json
import logging
import os
import re as _re
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
{"tool": "edit_file", "args": {"path": "src/main.py", "old_string": "def old_func():", "new_string": "def new_func():"}}
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
            "description": "Create or overwrite a file with new content.",
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
]


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
            clean_parts.append(remaining[idx:])
            break

        json_str = remaining[idx + len(start_tag):end_idx].strip()
        try:
            tool_data = json.loads(json_str)
            if isinstance(tool_data, dict) and "tool" in tool_data:
                tools.append(tool_data)
        except json.JSONDecodeError:
            pass

        remaining = remaining[end_idx + len(end_tag):]

    clean_text = "".join(clean_parts).strip()
    return clean_text, tools


def extract_json_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Extract JSON-style tool calls from model output.

    Supports plain-text function-call objects often emitted by local models,
    for example::

        {"name": "verify_changes", "arguments": {"command": "mypy ."}}

    Returns ``(clean_text, [tool_dicts])`` where each tool dict matches the
    internal shape ``{"tool": name, "args": {...}}``.
    """

    def _to_tool_call(obj: Any) -> dict[str, Any] | None:
        if not isinstance(obj, dict):
            return None

        # Native-ish format: {"name": "tool", "arguments": {...}}
        if "name" in obj and "arguments" in obj:
            name = obj.get("name")
            args = obj.get("arguments", {})
            if isinstance(name, str):
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if isinstance(args, dict):
                    return {"tool": name, "args": args}

        # Already normalized format
        if "tool" in obj and "args" in obj:
            name = obj.get("tool")
            args = obj.get("args", {})
            if isinstance(name, str) and isinstance(args, dict):
                return {"tool": name, "args": args}

        return None

    tools: list[dict[str, Any]] = []
    clean_text = text

    # First, inspect fenced JSON blocks.
    block_pattern = _re.compile(r"```(?:json)?\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```", _re.IGNORECASE)
    for match in block_pattern.finditer(text):
        candidate = match.group(1).strip()
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, list):
            for item in parsed:
                tc = _to_tool_call(item)
                if tc is not None:
                    tools.append(tc)
        else:
            tc = _to_tool_call(parsed)
            if tc is not None:
                tools.append(tc)

        clean_text = clean_text.replace(match.group(0), "").strip()

    # If nothing found, try parsing the whole response as one JSON object/list.
    if not tools:
        candidate = text.strip()
        if candidate.startswith("{") or candidate.startswith("["):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, list):
                    for item in parsed:
                        tc = _to_tool_call(item)
                        if tc is not None:
                            tools.append(tc)
                else:
                    tc = _to_tool_call(parsed)
                    if tc is not None:
                        tools.append(tc)
                if tools:
                    clean_text = ""
            except json.JSONDecodeError:
                pass

    return clean_text, tools


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
            return f"Error: Unknown tool '{tool_name}'"

        try:
            return handler(args)
        except Exception as exc:
            return f"Error: {exc}"

    # -- tool implementations ----------------------------------------------

    def _resolve_path(self, rel_path: str) -> Path:
        """Resolve a relative path safely within the repo."""
        target = (self.repo_path / rel_path).resolve()
        if not target.is_relative_to(self.repo_path):
            raise ValueError(f"Path traversal blocked: {rel_path!r}")
        return target

    def _read_file(self, args: dict[str, Any]) -> str:
        path = self._resolve_path(args.get("path", ""))
        if not path.is_file():
            return f"Error: File not found: {args.get('path')}"

        text = path.read_text(encoding="utf-8", errors="replace")
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
            return f"Error: File not found: {rel}"

        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        if not old_string:
            return "Error: old_string is required"

        text = path.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count == 0:
            return "Error: old_string not found in file"
        if count > 1:
            return f"Error: old_string matches {count} locations — be more specific"

        text = text.replace(old_string, new_string, 1)
        path.write_text(text, encoding="utf-8")
        return f"Successfully edited {rel}"

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
        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + "\n... (truncated)"
        return output

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
        kind = args.get("kind", None)
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
