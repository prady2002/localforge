"""Chat engine — provides an interactive REPL for conversing with the codebase."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner

from localforge.chat.session import ChatSession
from localforge.chat.tools import (
    TOOL_DESCRIPTIONS,
    TOOL_SCHEMAS,
    TOOL_SCHEMAS_FAST,
    ToolExecutor,
    extract_all_tool_calls,
    extract_json_tool_calls,
    hash_tool_call,
    validate_tool_call,
)
from localforge.core.config import LocalForgeConfig
from localforge.core.ollama_client import OllamaClient

logger = logging.getLogger(__name__)

console = Console()

# Short, focused system prompt — tool descriptions are sent via the native
# tools API parameter so they don't burn context tokens.
_SYSTEM_PROMPT = """\
You are LocalForge, an autonomous AI coding agent. You EXECUTE tasks directly.
You have access to tools that let you read files, edit files, run shell commands,
and search the codebase. USE THEM for every task.

CRITICAL RULES — you MUST follow these:
1. ACT immediately. Call tools to read files, run commands, edit code.
2. NEVER tell the user to do something manually. YOU do it with tools.
3. NEVER say "please review" or "let me know". YOU review and YOU decide.
4. When a command shows errors/warnings, YOU fix them by editing the files.
5. ITERATE: run command → read errors → edit files to fix → re-run to verify.
   Keep going until the command passes cleanly or you've fixed all issues.
6. Read the relevant files BEFORE editing so you have exact content to match.
7. Only give a brief summary AFTER all work is done and verified.
8. If linter output (ruff, mypy, etc.) shows problems, fix EVERY issue by
   editing the source files, then re-run the linter to confirm.

EDITING STRATEGY — avoid failures:
- ALWAYS read a file before editing it to get exact current content
- Include 3+ surrounding context lines in old_string for unique matching
- If edit_file fails with "matches N locations", add more context lines
- If edit_file fails with "not found", re-read the file — it may have changed
- Use edit_lines (line numbers) when you know exact line numbers from read_file
- For large changes, prefer apply_diff with unified diff format
- NEVER make a no-op edit where old_string equals new_string
- If stuck on the same error 2+ times, try a completely different approach
"""

# System prompt for analysis-only queries (no tool calling expected)
_ANALYSIS_SYSTEM_PROMPT = """\
You are LocalForge, an intelligent code analysis assistant. You provide insights about code.

RULES:
1. Answer questions directly and concisely.
2. If asked to explain code, provide clear explanations.
3. For analysis questions, give direct answers without excessive preamble.
4. Be helpful but brief.
"""

# Longer XML-format fallback prompt, used only when native tool calling fails.
_XML_FALLBACK_PROMPT = TOOL_DESCRIPTIONS

# Keywords that indicate action-oriented queries (need tool execution)
_ACTION_KEYWORDS = {
    "run", "execute", "fix", "edit", "delete", "create", "write",
    "add", "remove", "change", "modify", "refactor", "optimize",
    "install", "apply", "patch", "generate", "autofix", "implement",
    "make", "do", "build", "test", "setup", "configure",
}

# Keywords that indicate analysis-only queries (no tool execution needed)
_ANALYSIS_KEYWORDS = {
    "what", "how", "why", "when", "where", "explain", "describe",
    "analyze", "check", "find", "search", "list", "show", "tell",
    "count", "summarize", "review", "understand", "compare", "is",
    "could", "would", "can", "should", "does", "has", "get",
}

# Phrases that indicate the model is being lazy (giving instructions instead of acting)
_LAZY_INDICATORS = [
    "you can run",
    "you should run",
    "you could run",
    "try running",
    "you can use",
    "you should use",
    "run the following",
    "execute the following",
    "you need to",
    "you'll need to",
    "follow these steps",
    "here are the steps",
    "here's what you need to do",
    "here is what you need",
    "steps to fix",
    "to fix this, you",
    "to resolve this",
    "you can fix this by",
    "i recommend",
    "i suggest",
    "i would suggest",
    "i would recommend",
]


class ChatEngine:
    """Interactive chat engine backed by Ollama + codebase context + tools."""

    _MAX_TOOL_ROUNDS = 50  # allow extensive autonomous exploration & iteration
    _MAX_ANALYSIS_LENGTH = 2000  # max response length for analysis queries

    # Prompts that are usually direct terminal tasks and do not need expensive
    # repo-map/context retrieval before the first tool call.
    _FAST_ACTION_PREFIXES = (
        "run ",
        "execute ",
    )

    def __init__(
        self,
        config: LocalForgeConfig,
        ollama: OllamaClient,
        repo_path: Path,
    ) -> None:
        self.config = config
        self.ollama = ollama
        self.repo_path = repo_path
        self.session = ChatSession(
            session_id=hashlib.sha256(str(repo_path).encode()).hexdigest()[:12],
            repo_path=str(repo_path),
            model=config.model_name,
        )
        self._context_cache: str = ""
        self._repo_map_cache: str = ""
        self.tools = ToolExecutor(repo_path)
        self._token_count: int = 0  # rough token counter for session
        self._tool_calls_count: int = 0  # total tool calls executed
        self._rounds_count: int = 0  # total inference rounds

    @staticmethod
    def _classify_query(user_input: str) -> str:
        """Classify query as 'action' (needs tool execution) or 'analysis' (just answer).

        Returns: 'action' or 'analysis'
        """
        lower = user_input.lower()

        # Check for explicit action keywords
        action_count = sum(1 for kw in _ACTION_KEYWORDS if kw in lower)
        analysis_count = sum(1 for kw in _ANALYSIS_KEYWORDS if kw in lower)

        # Strong indicators override
        if any(phrase in lower for phrase in ["run ", "execute ", "fix ", "edit ", "create "]):
            return "action"

        if any(phrase in lower for phrase in ["what is", "how do", "explain", "analyze"]):
            return "analysis"

        # If query starts with question word, likely analysis
        question_starts = ("what", "how", "why", "when", "where", "can", "could", "would", "should")
        if lower.lstrip().startswith(question_starts):
            return "analysis"

        # Heuristic: more action keywords → action, more analysis keywords → analysis
        if action_count > analysis_count:
            return "action"
        elif analysis_count > action_count:
            return "analysis"

        # Default to action for ambiguous queries (safer to have tool loop available)
        return "action"

    def _get_session_path(self) -> Path:
        return self.repo_path / ".localforge" / "chat_history.json"

    def _recent_messages(
        self, max_messages: int = 16,
    ) -> list[dict[str, Any]]:
        """Return the last *max_messages* from the session as dicts.

        Keeps the most recent messages so the model has immediate context
        without paying the prompt-eval cost of the entire conversation.
        """
        msgs = self.session.messages
        if len(msgs) <= max_messages:
            return [{"role": m.role, "content": m.content} for m in msgs]
        return [
            {"role": m.role, "content": m.content}
            for m in msgs[-max_messages:]
        ]

    def _append_project_rules(self, system_parts: list[str]) -> None:
        """Append .localforge/rules.md content to *system_parts[0]* in-place."""
        rules_path = self.repo_path / ".localforge" / "rules.md"
        if rules_path.is_file():
            try:
                rules_content = rules_path.read_text(encoding="utf-8").strip()
                lines = [
                    ln for ln in rules_content.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                ]
                if lines:
                    system_parts[0] += f"\n\nPROJECT RULES:\n{rules_content}"
            except OSError:
                pass

    def _ensure_index(self) -> None:
        """Auto-index the repository if no index exists yet."""
        try:
            from localforge.index import RepositoryIndexer

            db_path = self.repo_path / self.config.index_db_path
            indexer = RepositoryIndexer(self.repo_path, db_path, self.config)
            try:
                if not indexer.is_initialized():
                    console.print("[yellow]No index found — indexing repository…[/yellow]")
                    stats = indexer.index_repository()
                    console.print(
                        f"[green]Indexed {stats['indexed']} files "
                        f"in {stats['duration_seconds']}s[/green]"
                    )
            finally:
                indexer.close()
        except Exception:
            logger.debug("Auto-index failed", exc_info=True)

    def _build_repo_map(self) -> str:
        """Build an intelligent repo map showing project structure and key definitions."""
        if self._repo_map_cache:
            return self._repo_map_cache

        _skip_dirs = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".localforge", ".tox", ".mypy_cache",
            ".pytest_cache", ".eggs", "*.egg-info",
        }

        lines: list[str] = ["PROJECT STRUCTURE:"]
        file_count = 0
        max_files = 200

        for dirpath, dirnames, filenames in os.walk(self.repo_path):
            dirnames[:] = [
                d for d in sorted(dirnames)
                if d not in _skip_dirs and not d.startswith(".")
            ]
            try:
                rel = Path(dirpath).relative_to(self.repo_path)
            except ValueError:
                continue
            depth = len(rel.parts)
            if depth > 5:
                continue

            indent = "  " * depth
            dir_name = rel.name if depth > 0 else str(self.repo_path.name)
            lines.append(f"{indent}{dir_name}/")

            for fname in sorted(filenames):
                if fname.startswith(".") or fname.endswith((".pyc", ".pyo")):
                    continue
                file_count += 1
                if file_count > max_files:
                    lines.append(f"{indent}  ... (more files)")
                    break
                lines.append(f"{indent}  {fname}")

            if file_count > max_files:
                break

        # Add symbol overview from index if available
        try:
            from localforge.index import IndexSearcher

            db_path = self.repo_path / self.config.index_db_path
            if db_path.is_file():
                searcher = IndexSearcher(db_path)
                try:
                    conn = searcher._get_conn()
                    # Get top-level classes and functions
                    rows = conn.execute(
                        """
                        SELECT s.name, s.kind, s.line, f.relative_path
                          FROM symbols s
                          JOIN files f ON f.id = s.file_id
                         WHERE s.scope = 'module' OR s.kind IN ('class', 'interface')
                         ORDER BY f.relative_path, s.line
                         LIMIT 150
                        """
                    ).fetchall()

                    if rows:
                        lines.append("\nKEY DEFINITIONS:")
                        by_file: dict[str, list[str]] = {}
                        for row in rows:
                            fp = row["relative_path"]
                            entry = f"  {row['kind']}: {row['name']} (L{row['line']})"
                            by_file.setdefault(fp, []).append(entry)
                        for fp, entries in sorted(by_file.items()):
                            lines.append(f"  {fp}")
                            lines.extend(entries[:10])
                finally:
                    searcher.close()
        except Exception:
            logger.debug("Could not load symbols for repo map", exc_info=True)

        self._repo_map_cache = "\n".join(lines)
        return self._repo_map_cache

    def load_session(self) -> bool:
        """Try to load a previous chat session. Returns True if loaded."""
        path = self._get_session_path()
        if path.is_file():
            try:
                self.session = ChatSession.load(path)
                return True
            except Exception:
                logger.debug("Could not load chat session", exc_info=True)
        return False

    def save_session(self) -> None:
        self.session.save(self._get_session_path())

    def _build_context(self, query: str, limit: int = 8) -> str:
        """Retrieve relevant codebase context for the user's query."""
        try:
            from localforge.index import IndexSearcher, RepositoryIndexer
            from localforge.retrieval import ContextRetriever

            db_path = self.repo_path / self.config.index_db_path
            if not db_path.is_file():
                # Try auto-indexing first
                self._ensure_index()
                if not db_path.is_file():
                    return ""

            indexer = RepositoryIndexer(self.repo_path, db_path, self.config)
            searcher = IndexSearcher(db_path)
            try:
                retriever = ContextRetriever(indexer, searcher, self.config)
                # Lower default retrieval fan-out keeps chat responsive while
                # still surfacing relevant snippets.
                result = retriever.retrieve(query, limit=max(1, limit))
                if not result.chunks:
                    return ""

                parts: list[str] = []
                for chunk in result.chunks:
                    parts.append(
                        f"--- {chunk.file_path} (L{chunk.start_line}-{chunk.end_line}) ---\n"
                        f"{chunk.content}"
                    )
                return "\n\n".join(parts)
            finally:
                searcher.close()
                indexer.close()
        except Exception:
            logger.debug("Context retrieval failed", exc_info=True)
            return ""

    @classmethod
    def _is_fast_action_query(cls, user_input: str) -> bool:
        """Return True when query is likely a SIMPLE, single-step command.

        Returns False when the user clearly asks for follow-up work (fix,
        edit, change, refactor…) because those need the full tool set,
        repo map, and more context.
        """
        lower = user_input.lower().strip()
        if not lower.startswith(cls._FAST_ACTION_PREFIXES):
            return False

        # If the prompt also requests edits / fixes, it's a multi-step task
        # and must NOT be treated as a fast action.
        _COMPLEX_SIGNALS = (
            "fix", "edit", "change", "modify", "refactor", "update",
            "rewrite", "add", "remove", "delete", "replace", "apply",
            "resolve", "correct", "patch", "all issues", "all errors",
            "and then", "after that",
        )
        if any(sig in lower for sig in _COMPLEX_SIGNALS):
            return False

        # Typical verification/lint/test commands should not pay the cost of
        # loading repo map + retrieved chunks before first tool call.
        command_hints = (
            "ruff",
            "pytest",
            "mypy",
            "black",
            "isort",
            "flake8",
            "pip ",
            "npm ",
            "pnpm ",
            "yarn ",
            "go test",
            "cargo",
            "gradle",
            "mvn",
        )
        return any(h in lower for h in command_hints)

    @classmethod
    def _is_tool_driven_query(cls, user_input: str) -> bool:
        """Return True when query is tool-driven and doesn't need upfront context.

        For queries like 'run ruff check . and fix all issues', the model
        discovers what to fix through tool output (ruff errors), not through
        repo context. Loading repo map + context just wastes tokens and
        slows prompt eval on small models.
        """
        lower = user_input.lower().strip()

        # Pattern: "run X and fix" / "check and fix" type queries
        tool_driven_patterns = (
            "ruff", "lint", "flake8", "pylint", "mypy",
            "pytest", "test", "check",
        )
        action_patterns = ("fix", "resolve", "correct", "repair")

        has_tool = any(t in lower for t in tool_driven_patterns)
        has_fix = any(a in lower for a in action_patterns)

        # "run ruff check . and fix all issues" → tool-driven
        if has_tool and has_fix:
            return True

        # "fix all ruff issues" → tool-driven
        if lower.startswith(("fix ", "resolve ", "correct ")):
            if has_tool:
                return True

        return False

    @classmethod
    def _is_scaffolding_query(cls, user_input: str) -> bool:
        """Return True when the user wants to build/create an entire project.

        For queries like 'build me a Flask todo app' or 'create a REST API',
        the model doesn't need repo context — it needs to scaffold from scratch.
        """
        lower = user_input.lower().strip()

        # Must have a creation intent
        creation_signals = (
            "build ", "create ", "make ", "scaffold ", "generate ",
            "set up ", "setup ", "initialize ", "init ", "bootstrap ",
            "start ", "new ", "from scratch",
        )
        has_creation = any(s in lower for s in creation_signals)
        if not has_creation:
            return False

        # Must mention an app/project/thing to build
        project_signals = (
            "app", "application", "project", "api", "website", "site",
            "server", "service", "bot", "cli", "tool", "library",
            "package", "module", "game", "dashboard", "frontend",
            "backend", "fullstack", "full-stack", "microservice",
            "crud", "rest", "graphql", "todo", "blog", "chat",
        )
        return any(s in lower for s in project_signals)

    @classmethod
    def _is_debugging_query(cls, user_input: str) -> bool:
        """Return True when the user is describing a bug to fix.

        For queries like 'users can\'t login' or 'the app crashes when...',
        the model needs repo context to find the bug.
        """
        lower = user_input.lower().strip()

        bug_signals = (
            "bug", "crash", "error", "broken", "doesn't work",
            "doesnt work", "not working", "fails", "failing",
            "issue", "problem", "wrong", "unexpected",
            "can't", "cant", "cannot", "won't", "wont",
            "exception", "traceback", "stack trace", "segfault",
        )
        return any(s in lower for s in bug_signals)

    async def send_message(self, user_input: str) -> str:
        """Send a message and get response optimized for query type.

        - ANALYSIS queries (what/how/why/explain) bypass tool loop for speed
        - ACTION queries use tool-calling with optimized retry logic
        - Uses Ollama's native tool calling API, falls back to XML if needed
        """
        self.session.add_user_message(user_input)

        # Classify query type to optimize routing
        query_type = self._classify_query(user_input)

        # For ANALYSIS queries, skip tool loop entirely
        if query_type == "analysis":
            return await self._handle_analysis_query(user_input)

        # For ACTION queries, use optimized tool loop
        return await self._handle_action_query(user_input)

    async def _handle_analysis_query(self, user_input: str) -> str:
        """Handle analysis/Q&A queries directly without tool loop."""

        # Only fetch expensive context when the query seems to reference code.
        # Simple greetings / generic questions skip retrieval entirely.
        _code_signals = (
            ".", "/", "file", "class", "function", "method", "module",
            "import", "error", "bug", "test", "code", "variable", "def ",
            "src", "lib", "config", "index", "model", "schema",
        )
        needs_context = any(s in user_input.lower() for s in _code_signals)

        repo_map = self._build_repo_map() if needs_context else ""
        context = self._build_context(user_input, limit=5) if needs_context else ""

        # Use simpler system prompt for analysis
        system = _ANALYSIS_SYSTEM_PROMPT
        if repo_map:
            system += f"\n\n{repo_map}"
        if context:
            system += f"\n\nRELEVANT CODE CONTEXT:\n{context}"

        # Load project rules
        self._append_project_rules(system_parts := [system])
        system = system_parts[0]

        # Cap history to keep payload small → faster prompt eval
        working_messages = self._recent_messages(max_messages=10)

        try:
            # Simple streaming call without tool loop
            spinner_live = Live(
                Spinner("dots", text="[bold cyan]localforge[/bold cyan] analyzing…"),
                refresh_per_second=10,
                transient=True,
                console=console,
            )
            spinner_live.start()

            parts: list[str] = []
            first_token_received = False

            try:
                stream = self.ollama.chat_stream_tokens(
                    working_messages,
                    system=system,
                    temperature=0.2,  # Lower temp for better analysis
                )
                async for token in stream:
                    if not first_token_received:
                        first_token_received = True
                        spinner_live.stop()
                        console.print("[bold green]localforge[/bold green] ", end="")
                    parts.append(token)
                    console.print(token, end="", highlight=False)

                if not first_token_received:
                    spinner_live.stop()
                    console.print()

            finally:
                with contextlib.suppress(Exception):
                    spinner_live.stop()
                if parts:  # If we got any response, print newline
                    if first_token_received:
                        console.print()

            response = "".join(parts)
            self._token_count += len(parts)

        except Exception as exc:
            console.print()
            console.print(
                f"[bold red]Error:[/bold red] {exc}\n"
                "[yellow]Tip: Make sure Ollama is running.[/yellow]"
            )
            response = "Error — please check Ollama is running."

        self.session.add_assistant_message(response)
        self.save_session()
        return response

    async def _handle_action_query(self, user_input: str) -> str:
        """Handle action-oriented queries with tool calling and retry logic."""
        # Build contextual system prompt. For direct command prompts, skip
        # expensive context assembly for faster first-token latency.
        fast_action = self._is_fast_action_query(user_input)
        tool_driven = self._is_tool_driven_query(user_input)
        scaffolding = self._is_scaffolding_query(user_input)
        debugging = self._is_debugging_query(user_input)
        skip_context = fast_action or tool_driven or scaffolding
        repo_map = "" if skip_context else self._build_repo_map()
        context = "" if skip_context else self._build_context(user_input, limit=6)
        system = _SYSTEM_PROMPT

        if fast_action:
            system += (
                "\n\nTASK MODE: FAST_COMMAND"
                "\n- Run the requested command immediately."
                "\n- If the output is clean (no errors), report success briefly."
                "\n- If the output shows errors/warnings, do NOT stop."
                "\n  Read the affected files, fix the issues, then re-run to verify."
            )
        elif scaffolding:
            system += (
                "\n\nTASK MODE: SCAFFOLDING"
                "\n- You are building a project FROM SCRATCH."
                "\n- Use write_file to create each file. Parent directories are created automatically."
                "\n- WORKFLOW: plan files → create each file with write_file → run tests/linter → fix issues."
                "\n- Create a COMPLETE, working project — not just stubs."
                "\n- Include: source code, config files (pyproject.toml, package.json, etc.), README."
                "\n- After creating all files, run the app or tests to verify it works."
                "\n- If tests fail, read the error, fix the code, and re-run."
                "\n- Generate production-quality code with proper error handling."
                "\n- Use write_file for EVERY file — do NOT tell the user to create files."
            )
        elif tool_driven:
            system += (
                "\n\nTASK MODE: TOOL_DRIVEN_FIX"
                "\n- Run the linter/checker command FIRST to discover issues."
                "\n- For auto-fixable issues, try running with --fix flag first."
                "\n- Then read affected files and fix remaining issues manually."
                "\n- WORKFLOW: run command → read errors → read affected files → "
                "\n  edit files to fix → re-run command to verify → repeat until clean."
                "\n- Fix ALL issues, not just the first few."
                "\n- After fixing, re-run the check to verify everything is clean."
            )
        elif debugging:
            system += (
                "\n\nTASK MODE: DEBUGGING"
                "\n- The user has described a bug or issue."
                "\n- WORKFLOW: search/grep for relevant code → read affected files → "
                "\n  understand the root cause → edit to fix → run tests to verify."
                "\n- Use grep_codebase and search_code to find relevant code FIRST."
                "\n- Read the full file context before making edits."
                "\n- After fixing, run tests or the app to verify the fix works."
                "\n- If tests don't exist, create a minimal test to verify the fix."
            )

        if repo_map:
            system += f"\n\n{repo_map}"
        if context:
            system += f"\n\nRELEVANT CODE CONTEXT:\n{context}"

        # Load project rules
        if not skip_context:
            self._append_project_rules(system_parts := [system])
            system = system_parts[0]

        # Cap session messages to keep payload small → much faster prompt eval.
        # Fast actions need almost no history; regular actions get more.
        if fast_action:
            msg_cap = 4
        elif tool_driven or scaffolding:
            msg_cap = 8
        else:
            msg_cap = 16
        working_messages: list[dict[str, Any]] = self._recent_messages(
            max_messages=msg_cap,
        )

        # Choose tool set: lean for fast/tool-driven/scaffolding, full for complex tasks
        use_lean = fast_action or tool_driven or scaffolding
        active_tool_schemas = TOOL_SCHEMAS_FAST if use_lean else TOOL_SCHEMAS

        final_response = ""
        nudge_count = 0
        max_nudges = 3  # Allow multiple nudges so the model stays on-task
        native_tools_supported = True
        max_rounds = 12 if fast_action else 50  # More rounds for complex tasks

        # ── Phase 1: Loop progress tracking ──────────────────────
        tool_call_history: dict[str, int] = {}  # hash → count of identical calls
        consecutive_error_rounds = 0  # Rounds with only errors, no successful edits
        consecutive_no_tool_rounds = 0  # Rounds where model didn't use tools
        successful_actions_count = 0  # Total successful tool calls
        parse_repair_used = False  # Only allow 1 parse repair per query
        task_verified_clean = False  # Set when verify/ruff passes clean

        for _round in range(max_rounds):
            # ── Spinner with round indicator ─────────────────────
            if _round == 0:
                spinner_text = "[bold cyan]localforge[/bold cyan] thinking…"
            else:
                spinner_text = (
                    f"[bold cyan]localforge[/bold cyan] working… (step {_round + 1})"
                )
            spinner_live = Live(
                Spinner("dots", text=spinner_text),
                refresh_per_second=10,
                transient=True,
                console=console,
            )
            spinner_live.start()

            # ── Stream from model ────────────────────────────────
            parts: list[str] = []
            tool_calls_out: list[dict[str, Any]] = []
            first_token_received = False
            console_prefix_printed = False

            try:
                if native_tools_supported:
                    stream = self.ollama.chat_with_tools_stream(
                        working_messages,
                        tools=active_tool_schemas,
                        system=system,
                        temperature=0.4,
                        tool_calls_out=tool_calls_out,
                        # For fast actions (run ruff, etc.) the model mostly
                        # emits a tool call JSON — cap output to avoid stalls.
                        # Cap output tokens: fast actions use short cap, others
                        # get generous limit. Avoids stalls on small models.
                        num_predict=2048 if fast_action else 4096,
                    )
                else:
                    stream = self.ollama.chat_stream_tokens(
                        working_messages,
                        system=system + "\n\n" + _XML_FALLBACK_PROMPT,
                        temperature=0.4,
                    )

                async for token in stream:
                    if not first_token_received:
                        first_token_received = True
                        spinner_live.stop()
                        console.print(
                            "[bold green]localforge[/bold green] ", end="",
                        )
                        console_prefix_printed = True
                    parts.append(token)
                    console.print(token, end="", highlight=False)

                if not first_token_received:
                    spinner_live.stop()
                if console_prefix_printed:
                    console.print()

            except Exception as exc:
                with contextlib.suppress(Exception):
                    spinner_live.stop()
                # If native tool calling failed with 400, fall back
                if native_tools_supported and "400" in str(exc):
                    native_tools_supported = False
                    logger.info("Native tool calling not supported, using fallback.")
                    continue
                console.print()
                console.print(
                    f"[bold red]Connection error:[/bold red] {exc}\n"
                    "[yellow]Tip: Make sure Ollama is running and the model "
                    "is loaded. Try 'ollama run <model>' first.[/yellow]"
                )
                self.session.add_assistant_message(
                    "Connection error — please check Ollama is running."
                )
                return "Connection error — please check Ollama."

            content = "".join(parts)
            self._token_count += len(parts)
            self._rounds_count += 1

            # ── Process tool calls from all paths ────────────────
            tool_calls: list[dict[str, Any]] = []
            is_native = False

            # Path 1: Native tool calls
            if tool_calls_out:
                is_native = True
                for tc in tool_calls_out:
                    func = tc.get("function", {})
                    tool_name = func.get("name", "unknown")
                    tool_args = func.get("arguments", {})
                    if isinstance(tool_args, str):
                        try:
                            tool_args = json.loads(tool_args)
                        except json.JSONDecodeError:
                            tool_args = {}
                    tool_calls.append({"tool": tool_name, "args": tool_args})

            # Path 2: XML fallback
            if not tool_calls:
                _, xml_calls = extract_all_tool_calls(content)
                tool_calls = xml_calls

            # Path 3: JSON fallback — skip when task is verified clean to avoid
            # picking up JSON-like text from the model's natural-language summary
            if not tool_calls and not task_verified_clean:
                _, json_calls = extract_json_tool_calls(content)
                tool_calls = json_calls

            # ── Execute tool calls if any ────────────────────────
            if tool_calls:
                nudge_count = 0
                consecutive_no_tool_rounds = 0
                round_had_error = True  # Assume error until proven otherwise
                round_had_success = False

                if not is_native and native_tools_supported:
                    native_tools_supported = False
                    logger.info("Using fallback text-based tool calls.")

                all_results: list[tuple[str, str]] = []

                for i, tc in enumerate(tool_calls, 1):
                    tool_name = tc.get("tool", "unknown")
                    tool_args = tc.get("args", {})

                    # Phase 2: Validate tool call before executing
                    validation_err = validate_tool_call(tc)
                    if validation_err:
                        console.print(
                            f"  [dim]⚡ Tool {tool_name}[/dim]",
                        )
                        console.print(
                            f"  [dim]   ✗ Validation: {validation_err}[/dim]",
                        )
                        all_results.append((tool_name, f"Error: {validation_err}"))
                        continue

                    # Phase 1: Check for repeated identical tool calls
                    call_hash = hash_tool_call(tool_name, tool_args)
                    tool_call_history[call_hash] = tool_call_history.get(call_hash, 0) + 1

                    if tool_call_history[call_hash] > 2:
                        console.print(
                            f"  [yellow]⚠ Skipping repeated tool call: "
                            f"{tool_name} (called {tool_call_history[call_hash]}x with same args)[/yellow]",
                        )
                        all_results.append((
                            tool_name,
                            f"Error: You've called {tool_name} with identical arguments "
                            f"{tool_call_history[call_hash]} times. The result will be the same. "
                            "Try a different approach — read the file for exact content, "
                            "use edit_lines with line numbers, or change your strategy.",
                        ))
                        continue

                    label = (
                        f"[{i}/{len(tool_calls)}]"
                        if len(tool_calls) > 1
                        else ""
                    )
                    console.print(
                        f"  [dim]⚡ Tool {label} {tool_name}[/dim]", end="",
                    )
                    self._print_tool_arg_preview(tool_name, tool_args)

                    start_t = time.monotonic()
                    result = self.tools.execute(tool_name, tool_args)
                    elapsed = time.monotonic() - start_t

                    # Track success/error for the round
                    if not result.startswith("Error:"):
                        round_had_success = True
                        round_had_error = False
                        successful_actions_count += 1
                        self._tool_calls_count += 1

                        # If an edit/write succeeded, the project state changed.
                        # Reset dedup counts for run_command/verify_changes so
                        # the model can re-run linters/tests to verify its edits.
                        _EDIT_TOOLS = {"edit_file", "edit_lines", "write_file", "batch_edit", "apply_diff"}
                        if tool_name in _EDIT_TOOLS:
                            stale = [h for h, _cnt in tool_call_history.items()
                                     if _cnt > 1]
                            for h in stale:
                                tool_call_history[h] = 1

                        # Detect when verification passes clean — task is done
                        _clean_markers = (
                            "all checks passed", "all_checks_passed",
                            "no issues", "found 0 error",
                            "0 error(s)",  # msbuild / dotnet
                        )
                        result_lower = result.lower()
                        is_clean = any(m in result_lower for m in _clean_markers)
                        # pytest: "4 passed in 0.10s" with successful exit
                        if not is_clean and " passed" in result_lower and "(exit code:" not in result_lower:
                            is_clean = True
                        if tool_name in ("verify_changes", "run_command") and is_clean:
                            task_verified_clean = True

                    preview = result[:150].replace("\n", " ")
                    if len(result) > 150:
                        preview += "…"
                    status_icon = "✓" if not result.startswith("Error:") else "✗"
                    console.print(
                        f"  [dim]   {status_icon} ({elapsed:.1f}s) {preview}[/dim]",
                    )
                    all_results.append((tool_name, result))

                console.print()

                # Phase 1: Track consecutive error rounds
                if round_had_error and not round_had_success:
                    consecutive_error_rounds += 1
                else:
                    consecutive_error_rounds = 0

                # Phase 1: Stuck detection
                if consecutive_error_rounds >= 3:
                    console.print(
                        "\n  [bold yellow]⚠ Stuck detected:[/bold yellow] "
                        "3 consecutive rounds with only errors. "
                        "Injecting recovery prompt…\n",
                    )
                    consecutive_error_rounds = 0  # Reset to give it another chance
                    working_messages.append(
                        {"role": "assistant", "content": content},
                    )
                    # Add all results
                    result_text = "\n\n".join(
                        f"Tool result for {name}:\n```\n{res}\n```"
                        for name, res in all_results
                    )
                    working_messages.append({
                        "role": "user",
                        "content": (
                            f"{result_text}\n\n"
                            "IMPORTANT: You have been stuck for multiple rounds making the same errors. "
                            "STOP and reconsider your approach:\n"
                            "1. Re-read the target file(s) with read_file to get EXACT current content\n"
                            "2. Use edit_lines with line numbers instead of edit_file if string matching fails\n"
                            "3. Try a completely different strategy to achieve the goal\n"
                            "4. If you truly cannot proceed, explain what's blocking you\n"
                            "Do NOT repeat the same failed operations."
                        ),
                    })
                    continue

                # Build messages for next round
                if is_native:
                    assistant_msg: dict[str, Any] = {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls_out,
                    }
                    working_messages.append(assistant_msg)
                    for _tool_name, result in all_results:
                        working_messages.append({
                            "role": "tool",
                            "content": result,
                        })
                else:
                    working_messages.append(
                        {"role": "assistant", "content": content},
                    )
                    result_text = "\n\n".join(
                        f"Tool result for {name}:\n```\n{res}\n```"
                        for name, res in all_results
                    )
                    working_messages.append({
                        "role": "user",
                        "content": result_text + "\n\nContinue. Keep using tools until done.",
                    })
                continue

            # ── No tool calls extracted ─────────────────────────
            consecutive_no_tool_rounds += 1

            # Phase 2: Parse repair — try once if all extraction failed
            # Skip parse repair when the task has already been verified clean
            if (
                not parse_repair_used
                and not task_verified_clean
                and content.strip()
                and (
                    "{" in content
                    or "tool" in content.lower()
                    or "edit" in content.lower()
                    or "read" in content.lower()
                    or "run" in content.lower()
                )
            ):
                parse_repair_used = True
                console.print(
                    "\n  [yellow]↻ Could not parse tool calls. Asking model to reformat…[/yellow]",
                )
                working_messages.append(
                    {"role": "assistant", "content": content},
                )
                working_messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response could not be parsed as tool calls. "
                        "You MUST wrap each tool call in <tool_call> tags like this:\n\n"
                        "<tool_call>\n"
                        '{"tool": "tool_name", "args": {"arg1": "value"}}\n'
                        "</tool_call>\n\n"
                        "Reformat your intended action using the exact format above. "
                        "Do NOT explain — just output the <tool_call> tags."
                    ),
                })
                continue

            # ── Lazy / premature-stop detection ────────────────────
            # Skip these checks when the task has been verified clean —
            # the model is giving a legitimate summary, not being lazy.
            if (
                not task_verified_clean
                and self._is_lazy_response(content)
                and nudge_count < max_nudges
            ):
                nudge_count += 1
                console.print(
                    "\n  [yellow]↻ Agent not using tools. Nudging to act…[/yellow]",
                )
                working_messages.append(
                    {"role": "assistant", "content": content},
                )
                working_messages.append({
                    "role": "user",
                    "content": (
                        "STOP. You must NOT give instructions to the user. "
                        "You are an autonomous agent — use your tools NOW. "
                        "Read the relevant files with read_file, then fix "
                        "the issues with edit_file. Keep going until done."
                    ),
                })
                continue

            # Even if the response isn't obviously lazy, catch the "please
            # review" / "let me know" hand-off that local models love to do.
            if (
                not task_verified_clean
                and nudge_count < max_nudges
                and self._is_premature_handoff(content)
            ):
                nudge_count += 1
                console.print(
                    "\n  [yellow]↻ Agent tried to hand off. Continuing…[/yellow]",
                )
                working_messages.append(
                    {"role": "assistant", "content": content},
                )
                working_messages.append({
                    "role": "user",
                    "content": (
                        "Do NOT ask me to review. YOU must continue working. "
                        "If there are errors or issues, use edit_file to fix "
                        "them, then run the command again to verify."
                    ),
                })
                continue

            # ── Done — final response ────────────────────────────────
            final_response = content
            break
        else:
            # Max rounds reached — provide diagnostic
            console.print(
                f"[yellow]Maximum tool rounds ({max_rounds}) reached. "
                f"Completed {successful_actions_count} successful actions.[/yellow]",
            )
            final_response = content if content else "(no response)"

        # Persist to session
        self.session.add_assistant_message(final_response)
        self.save_session()
        return final_response

    @staticmethod
    def _print_tool_arg_preview(
        tool_name: str, tool_args: dict[str, Any],
    ) -> None:
        """Print a short preview of tool arguments."""
        _arg_preview = ""
        if tool_name in ("read_file", "edit_file", "write_file", "edit_lines", "apply_diff"):
            _arg_preview = tool_args.get("path", "")
            if tool_name == "edit_lines":
                sl = tool_args.get("start_line", "?")
                el = tool_args.get("end_line", "?")
                _arg_preview += f" L{sl}-{el}"
        elif tool_name == "run_command":
            _arg_preview = tool_args.get("command", "")[:80]
        elif tool_name in ("search_code", "grep_codebase"):
            _arg_preview = tool_args.get("pattern", "")[:60]
        elif tool_name == "verify_changes":
            _arg_preview = tool_args.get("command", "auto-detect")
        elif tool_name == "find_symbols":
            _arg_preview = tool_args.get("name", "")
        if _arg_preview:
            console.print(f" [dim]({_arg_preview})[/dim]")
        else:
            console.print()

    @staticmethod
    def _is_lazy_response(response: str) -> bool:
        """Detect if model gave instructions instead of using tools.

        Only flags very obvious cases - no nuance about analysis responses.
        """
        lower = response.lower()

        # If response is short, probably a direct answer (not lazy)
        if len(response.strip()) < 100:
            return False

        # Very strong signal: numbered steps with imperative verbs
        import re
        step_pattern = re.findall(r'^\s*\d+\.\s+(?:run|execute|install|create|edit|delete|modify|open|use)\b', lower, re.MULTILINE)
        if len(step_pattern) >= 2:
            return True

        # Shell code blocks with no tool calls is lazy
        has_code_block = "```bash" in lower or "```shell" in lower
        if has_code_block and len(step_pattern) >= 1:
            return True

        # Check for strong lazy phrases (but require multiple)
        lazy_phrases = sum(1 for phrase in [
            "you can run", "you should run", "you should also run",
            "try running",
            "you can use", "you should use",
            "you need to", "you'll need to",
            "follow these steps", "here are the steps",
            "please review", "let me know",
            "you might want", "you may want",
            "here's how", "here is how",
            "manually",
        ] if phrase in lower)

        # A code block combined with any lazy phrase is a strong signal
        if has_code_block and lazy_phrases >= 1:
            return True

        # Single strong delegation phrase is lazy if the response is long
        if lazy_phrases >= 1 and len(response.strip()) > 300:
            return True

        return lazy_phrases >= 2

    @staticmethod
    def _is_premature_handoff(response: str) -> bool:
        """Detect when the model tries to hand control back to the user.

        This catches the very common local-model pattern of running one
        command and then saying "Please review the output" or "Let me know
        if you need anything else" instead of continuing to work.
        """
        lower = response.lower()

        _HANDOFF_PHRASES = (
            "please review",
            "let me know",
            "if you need",
            "if you want me to",
            "if you'd like me to",
            "would you like me to",
            "do you want me to",
            "i can help",
            "i can assist",
            "feel free to",
            "want me to",
            "need any help",
            "need further",
            "need additional",
            "any questions",
            "anything else",
            "specific issues",
            "manual attention",
            "provide more details",
        )
        return any(phrase in lower for phrase in _HANDOFF_PHRASES)

    async def run_repl(self) -> None:
        """Run the interactive chat REPL."""
        # Auto-index the repository if needed
        self._ensure_index()

        # Auto-detect context window from Ollama and keep models loaded.
        # detect_context_window now stores the value inside OllamaClient so
        # every subsequent payload automatically gets the correct num_ctx.
        try:
            detected = await self.ollama.detect_context_window()
            console.print(
                f"[dim]Model context window: {detected:,} tokens[/dim]"
            )
        except Exception:
            pass

        # Detect model capabilities (tool calling, JSON mode, family)
        try:
            caps = await self.ollama.detect_capabilities()
            console.print(
                f"[dim]Model family: {caps.get('family', 'unknown')} | "
                f"Tools: {'yes' if caps.get('supports_tools') else 'no'} | "
                f"JSON mode: {'yes' if caps.get('supports_json_mode') else 'no'}[/dim]"
            )
        except Exception:
            pass

        # Pre-load the model into Ollama VRAM/RAM so the first user prompt
        # doesn't stall while the model is being loaded from disk.
        try:
            with contextlib.suppress(Exception):
                await self.ollama.preload_model()
        except Exception:
            pass

        # Build and cache the repo map
        self._build_repo_map()

        # Try to load previous session
        loaded = self.load_session()
        if loaded and self.session.messages:
            n = len(self.session.messages)
            console.print(
                f"[dim]Resumed session with {n} message(s). "
                f"Type /clear to start fresh.[/dim]"
            )

        console.print(
            Panel(
                "[bold]LocalForge Chat[/bold] — autonomous coding agent\n"
                f"Model: [cyan]{self.config.model_name}[/cyan]  "
                f"Repo: [cyan]{self.repo_path}[/cyan]\n\n"
                "[dim]I can read/write/edit files, run any command, search code, and verify changes.\n"
                "Just tell me what to do and I'll execute it autonomously.\n"
                "Commands: /clear, /run, /read, /context, /tokens, /help, /quit[/dim]",
                border_style="cyan",
                expand=False,
            )
        )

        while True:
            try:
                user_input = console.input("\n[bold cyan]you >[/bold cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Goodbye![/dim]")
                break

            if not user_input:
                continue

            # Handle slash commands
            if user_input.startswith("/"):
                if await self._handle_command(user_input):
                    continue
                else:
                    break

            await self.send_message(user_input)

    async def _handle_command(self, cmd: str) -> bool:
        """Handle a slash command. Returns False if the REPL should exit."""
        parts = cmd.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if command in ("/quit", "/exit", "/q"):
            console.print("[dim]Goodbye![/dim]")
            return False

        elif command == "/clear":
            self.session.clear()
            self.save_session()
            console.print("[green]Chat history cleared.[/green]")

        elif command == "/history":
            if not self.session.messages:
                console.print("[dim]No messages yet.[/dim]")
            else:
                for i, msg in enumerate(self.session.messages, 1):
                    role_style = "cyan" if msg.role == "user" else "green"
                    label = "you" if msg.role == "user" else "localforge"
                    preview = msg.content[:120].replace("\n", " ")
                    console.print(
                        f"  [{role_style}]{i}. {label}:[/{role_style}] {preview}…"
                        if len(msg.content) > 120
                        else f"  [{role_style}]{i}. {label}:[/{role_style}] {msg.content}"
                    )

        elif command == "/context":
            if not arg:
                console.print("[yellow]Usage: /context <search query>[/yellow]")
            else:
                context = self._build_context(arg)
                if context:
                    console.print(Panel(
                        context[:3000],
                        title="Retrieved Context",
                        border_style="blue",
                    ))
                else:
                    console.print("[dim]No relevant context found.[/dim]")

        elif command == "/run":
            if not arg:
                console.print("[yellow]Usage: /run <command>[/yellow]")
            else:
                result = self.tools.execute("run_command", {"command": arg})
                console.print(Panel(result[:3000], title=f"$ {arg}", border_style="green"))

        elif command == "/read":
            if not arg:
                console.print("[yellow]Usage: /read <file path>[/yellow]")
            else:
                result = self.tools.execute("read_file", {"path": arg})
                console.print(Panel(result[:3000], title=arg, border_style="blue"))

        elif command == "/help":
            console.print(
                Panel(
                    "[bold]/clear[/bold]     — Clear chat history\n"
                    "[bold]/context[/bold]   — Search codebase for context\n"
                    "[bold]/history[/bold]   — Show conversation history\n"
                    "[bold]/run <cmd>[/bold] — Run a shell command directly\n"
                    "[bold]/read <path>[/bold] — Read a file\n"
                    "[bold]/tokens[/bold]    — Show token usage for this session\n"
                    "[bold]/help[/bold]      — Show this help\n"
                    "[bold]/quit[/bold]      — Exit chat\n\n"
                    "[dim]The AI can also use tools autonomously: read files,\n"
                    "edit code, run commands, and search the codebase.[/dim]",
                    title="Chat Commands",
                    border_style="cyan",
                    expand=False,
                )
            )

        elif command == "/tokens":
            n_msgs = len(self.session.messages)
            ctx_used_pct = (
                (self._token_count / self.config.max_context_tokens * 100)
                if self.config.max_context_tokens
                else 0
            )
            console.print(
                f"  [bold]Messages:[/bold] {n_msgs}  |  "
                f"[bold]Tokens (approx):[/bold] {self._token_count:,}  |  "
                f"[bold]Context window:[/bold] {self.config.max_context_tokens:,}  |  "
                f"[bold]Utilization:[/bold] {ctx_used_pct:.0f}%\n"
                f"  [bold]Tool calls:[/bold] {self._tool_calls_count}  |  "
                f"[bold]Inference rounds:[/bold] {self._rounds_count}"
            )

        else:
            console.print(f"[yellow]Unknown command: {command}. Type /help for options.[/yellow]")

        return True
