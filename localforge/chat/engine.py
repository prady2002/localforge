"""Chat engine — provides an interactive REPL for conversing with the codebase."""

from __future__ import annotations

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
from rich.text import Text

from localforge.chat.session import ChatSession
from localforge.chat.tools import (
    TOOL_DESCRIPTIONS,
    TOOL_SCHEMAS,
    ToolExecutor,
    extract_all_tool_calls,
    extract_json_tool_calls,
)
from localforge.core.config import LocalForgeConfig
from localforge.core.ollama_client import OllamaClient

logger = logging.getLogger(__name__)

console = Console()

# Short, focused system prompt — tool descriptions are sent via the native
# tools API parameter so they don't burn context tokens.
_SYSTEM_PROMPT = """\
You are LocalForge, an autonomous AI coding agent. You EXECUTE tasks — never give instructions.
You have direct access to the user's codebase through tools. USE THEM.

RULES:
1. ACT immediately. Call tools to read files, run commands, edit code.
2. NEVER tell the user to do something. YOU do it.
3. After code changes, ALWAYS verify (run tests/linters). Fix failures and retry.
4. Search/read the codebase before making changes.
5. Only give a brief summary AFTER all work is done and verified.
"""

# Longer XML-format fallback prompt, used only when native tool calling fails.
_XML_FALLBACK_PROMPT = TOOL_DESCRIPTIONS

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

    def _get_session_path(self) -> Path:
        return self.repo_path / ".localforge" / "chat_history.json"

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

        _SKIP_DIRS = {
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
                if d not in _SKIP_DIRS and not d.startswith(".")
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

    def _build_context(self, query: str) -> str:
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
                result = retriever.retrieve(query, limit=15)
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

    async def send_message(self, user_input: str) -> str:
        """Send a message and get a response, executing tool calls as needed.

        Uses Ollama's native tool calling API for reliable function calling.
        Falls back to XML ``<tool_call>`` parsing for models that don't support
        native tools.  Tool interactions are kept in a local working buffer
        and only the final summary is persisted to the session.
        """
        self.session.add_user_message(user_input)

        # Build contextual system prompt (compact — tool schemas sent via API)
        repo_map = self._build_repo_map()
        context = self._build_context(user_input)
        system = _SYSTEM_PROMPT

        if repo_map:
            system += f"\n\n{repo_map}"
        if context:
            system += f"\n\nRELEVANT CODE CONTEXT:\n{context}"

        # Load project rules
        rules_path = self.repo_path / ".localforge" / "rules.md"
        if rules_path.is_file():
            try:
                rules_content = rules_path.read_text(encoding="utf-8").strip()
                lines = [
                    ln for ln in rules_content.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                ]
                if lines:
                    system += f"\n\nPROJECT RULES:\n{rules_content}"
            except OSError:
                pass

        # Working message buffer — separate from session persistence.
        # Start from session history (includes the user message we just added).
        working_messages: list[dict[str, Any]] = [
            {"role": m.role, "content": m.content}
            for m in self.session.messages
        ]

        final_response = ""
        nudge_count = 0
        max_nudges = 3
        native_tools_supported = True  # optimistic; disabled on first failure

        for _round in range(self._MAX_TOOL_ROUNDS):
            # ── Spinner ──────────────────────────────────────────────
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

            # ── Stream from model (native tool calling) ──────────────
            parts: list[str] = []
            tool_calls_out: list[dict[str, Any]] = []
            first_token_received = False
            console_prefix_printed = False

            try:
                if native_tools_supported:
                    stream = self.ollama.chat_with_tools_stream(
                        working_messages,
                        tools=TOOL_SCHEMAS,
                        system=system,
                        temperature=0.4,
                        tool_calls_out=tool_calls_out,
                    )
                else:
                    # Fallback: plain streaming with XML tool descriptions
                    # injected into the system prompt.
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
                    console.print()  # final newline

            except Exception as exc:
                try:
                    spinner_live.stop()
                except Exception:
                    pass
                # If native tool calling caused a 400 error, fall back
                if native_tools_supported and "400" in str(exc):
                    native_tools_supported = False
                    logger.info(
                        "Native tool calling not supported, falling back to XML",
                    )
                    continue
                console.print()
                console.print(
                    f"[bold red]Connection error:[/bold red] {exc}\n"
                    "[yellow]Tip: Make sure Ollama is running and the model "
                    "is loaded. Try 'ollama run <model>' first.[/yellow]"
                )
                self.session.add_assistant_message(
                    "I encountered a connection error. Please check that "
                    "Ollama is running."
                )
                return "Connection error — please check Ollama."

            content = "".join(parts)
            self._token_count += len(parts)

            # ── Path 1: Native tool calls ────────────────────────────
            if tool_calls_out:
                nudge_count = 0
                all_results: list[tuple[str, str]] = []

                for i, tc in enumerate(tool_calls_out, 1):
                    func = tc.get("function", {})
                    tool_name = func.get("name", "unknown")
                    tool_args = func.get("arguments", {})

                    # arguments may be a JSON string
                    if isinstance(tool_args, str):
                        try:
                            tool_args = json.loads(tool_args)
                        except json.JSONDecodeError:
                            tool_args = {}

                    label = (
                        f"[{i}/{len(tool_calls_out)}] "
                        if len(tool_calls_out) > 1
                        else ""
                    )
                    console.print(
                        f"  [dim]⚡ Tool {label}{tool_name}[/dim]", end="",
                    )
                    self._print_tool_arg_preview(tool_name, tool_args)

                    start_t = time.monotonic()
                    result = self.tools.execute(tool_name, tool_args)
                    elapsed = time.monotonic() - start_t

                    preview = result[:200].replace("\n", " ")
                    if len(result) > 200:
                        preview += "…"
                    console.print(
                        f"  [dim]   ✓ ({elapsed:.1f}s) {preview}[/dim]",
                    )
                    all_results.append((tool_name, result))

                console.print()

                # Append assistant message (with tool_calls) to working buffer
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls_out,
                }
                working_messages.append(assistant_msg)

                # Append each tool result as 'tool' role
                for tool_name, result in all_results:
                    working_messages.append({
                        "role": "tool",
                        "content": result,
                    })
                continue

            # ── Path 2: XML/JSON fallback ────────────────────────────
            _, xml_tool_calls = extract_all_tool_calls(content)
            _, json_tool_calls = extract_json_tool_calls(content)
            fallback_tool_calls = xml_tool_calls or json_tool_calls
            if fallback_tool_calls:
                nudge_count = 0
                # First time we see XML tools, disable native for this session
                if native_tools_supported:
                    native_tools_supported = False
                    logger.info(
                        "Model uses fallback text tool calls, switching to fallback mode.",
                    )

                all_results_xml: list[str] = []
                for i, tc in enumerate(fallback_tool_calls, 1):
                    tool_name = tc.get("tool", "unknown")
                    tool_args = tc.get("args", {})

                    label = (
                        f"[{i}/{len(fallback_tool_calls)}] "
                        if len(fallback_tool_calls) > 1
                        else ""
                    )
                    console.print(
                        f"  [dim]⚡ Tool {label}{tool_name}[/dim]", end="",
                    )
                    self._print_tool_arg_preview(tool_name, tool_args)

                    start_t = time.monotonic()
                    result = self.tools.execute(tool_name, tool_args)
                    elapsed = time.monotonic() - start_t

                    preview = result[:200].replace("\n", " ")
                    if len(result) > 200:
                        preview += "…"
                    console.print(
                        f"  [dim]   ✓ ({elapsed:.1f}s) {preview}[/dim]",
                    )
                    all_results_xml.append(
                        f"Tool result for {tool_name}:\n```\n{result}\n```"
                    )

                console.print()

                # Feed results back as user messages (XML models expect this)
                working_messages.append(
                    {"role": "assistant", "content": content},
                )
                combined = "\n\n".join(all_results_xml)
                working_messages.append({
                    "role": "user",
                    "content": (
                        f"{combined}\n\nContinue working. Keep using tools "
                        "until the task is fully complete."
                    ),
                })
                continue

            # ── Path 3: Lazy response detection ──────────────────────
            if self._is_lazy_response(content) and nudge_count < max_nudges:
                nudge_count += 1
                console.print(
                    f"\n  [yellow]↻ Agent gave instructions instead of acting. "
                    f"Nudging… ({nudge_count}/{max_nudges})[/yellow]",
                )
                working_messages.append(
                    {"role": "assistant", "content": content},
                )
                working_messages.append({
                    "role": "user",
                    "content": (
                        "STOP. Do NOT give me instructions. "
                        "You are an autonomous agent — use your tools NOW. "
                        "Call run_command, read_file, edit_file, etc. DO IT."
                    ),
                })
                continue

            # ── Done — this is the final response ────────────────────
            final_response = content
            break
        else:
            console.print("[yellow]Maximum tool rounds reached.[/yellow]")
            final_response = content if content else "(no response)"

        # Persist only user message + final summary to session
        self.session.add_assistant_message(final_response)
        self.save_session()
        return final_response

    @staticmethod
    def _print_tool_arg_preview(
        tool_name: str, tool_args: dict[str, Any],
    ) -> None:
        """Print a short preview of tool arguments."""
        _arg_preview = ""
        if tool_name in ("read_file", "edit_file", "write_file"):
            _arg_preview = tool_args.get("path", "")
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
        """Detect if the model gave instructions instead of using tools."""
        lower = response.lower()
        # If response is very short, it's likely a direct answer (not lazy)
        if len(response.strip()) < 80:
            return False
        # Check for lazy phrasings
        matches = sum(1 for phrase in _LAZY_INDICATORS if phrase in lower)
        # Also check for backtick command suggestions (telling user to run something)
        if "```bash" in lower or "```shell" in lower or "```sh" in lower:
            matches += 2
        # If it contains numbered steps like "1." "2." "3." that's instructions
        import re
        step_pattern = re.findall(r"^\s*\d+\.\s", response, re.MULTILINE)
        if len(step_pattern) >= 3:
            matches += 2
        return matches >= 2

    async def run_repl(self) -> None:
        """Run the interactive chat REPL."""
        # Auto-index the repository if needed
        self._ensure_index()

        # Auto-detect context window from Ollama
        try:
            detected = await self.ollama.detect_context_window()
            if detected > self.config.max_context_tokens:
                console.print(
                    f"[dim]Detected model context window: {detected:,} tokens "
                    f"(using {self.config.max_context_tokens:,})[/dim]"
                )
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
            console.print(
                f"  [bold]Messages:[/bold] {n_msgs}  |  "
                f"[bold]Tokens (approx):[/bold] {self._token_count:,}  |  "
                f"[bold]Context window:[/bold] {self.config.max_context_tokens:,}"
            )

        else:
            console.print(f"[yellow]Unknown command: {command}. Type /help for options.[/yellow]")

        return True
