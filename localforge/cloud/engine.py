"""Cloud chat engine — provides an interactive REPL powered by the cloud API.

This is the enhanced counterpart of ``localforge/chat/engine.py``, designed
for the Gemini 3.1 Pro cloud model with 128K context, fast inference, and
strong reasoning.  It reuses ``ToolExecutor`` and the retrieval / index
infrastructure from the local engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner

from localforge.chat.tools import (
    ToolExecutor,
    extract_all_tool_calls,
    extract_json_tool_calls,
    hash_tool_call,
    validate_tool_call,
)
from localforge.cloud.client import THINKING_TOKEN_PREFIX, CloudClient
from localforge.cloud.exceptions import AuthExpiredError, VPNError
from localforge.cloud.prompts import (
    CLOUD_ANALYSIS_PROMPT,
    CLOUD_DEBUGGING_PROMPT,
    CLOUD_LARGE_SCAFFOLDING_PROMPT,
    CLOUD_SCAFFOLDING_PROMPT,
    CLOUD_SYSTEM_PROMPT,
    CLOUD_TEST_FIX_PROMPT,
    CLOUD_TOOL_PROMPT,
)
from localforge.cloud.session import CloudChatSession
from localforge.core.config import LocalForgeConfig

logger = logging.getLogger(__name__)
console = Console()

# Much larger limits for the cloud model
_MAX_TOOL_RESULT_CHARS = 50_000


def _truncate_tool_result(text: str, max_chars: int = _MAX_TOOL_RESULT_CHARS) -> str:
    """Truncate long tool results, keeping head + tail with an omission marker.

    The head gets 60% (most useful context is at the start) and the tail
    gets 30%, leaving ~10% for the separator/marker text.
    """
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = int(max_chars * 0.3)
    assert head + tail < max_chars, f"head({head}) + tail({tail}) >= max_chars({max_chars})"
    omitted = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n... ({omitted} characters omitted) ...\n"
        "WARNING: Truncated. Use read_file with start_line/end_line for exact content.\n\n"
        + text[-tail:]
    )


def _compress_create_project_result(result: str) -> str:
    """Compress create_project output — keep summary + tree, skip per-file details."""
    lines = result.splitlines()
    compressed: list[str] = []
    in_tree = False
    in_next_steps = False
    for line in lines:
        # Always keep the summary line
        if line.startswith("Created project"):
            compressed.append(line)
            continue
        # Keep PROJECT TREE section
        if "PROJECT TREE" in line:
            in_tree = True
            compressed.append(line)
            continue
        if in_tree:
            compressed.append(line)
            if not line.strip():
                in_tree = False
            continue
        # Keep NEXT STEPS section
        if "NEXT STEPS" in line:
            in_next_steps = True
            compressed.append(line)
            continue
        if in_next_steps:
            compressed.append(line)
            continue
        # Skip individual file success lines (✓) to save context
        if line.strip().startswith("✓"):
            continue
        # Keep error lines
        if line.strip().startswith("✗"):
            compressed.append(line)
    return "\n".join(compressed)


def _prune_working_messages(messages: list[dict[str, Any]], max_chars: int = 100_000) -> list[dict[str, Any]]:
    """Prune old messages to keep working context under budget.

    Keeps the first message (user's original request) and the most recent
    messages, dropping older tool result rounds from the middle.  Inserts
    a summary marker so the model knows context was pruned.
    """
    total = sum(len(m.get("content", "")) for m in messages)
    if total <= max_chars:
        return messages

    if len(messages) <= 4:
        return messages

    # Keep first 2 messages (original request + first response) and last N
    # Drop from the middle (older tool result rounds)
    first = messages[:2]
    rest = messages[2:]
    dropped_count = 0

    while total > max_chars and len(rest) > 4:
        dropped = rest.pop(0)
        total -= len(dropped.get("content", ""))
        dropped_count += 1

    # Insert a marker so the model knows messages were pruned
    if dropped_count > 0:
        marker = {
            "role": "user",
            "content": (
                f"[Context pruned: {dropped_count} earlier messages removed to stay within "
                f"context budget. The original request and most recent messages are preserved.]"
            ),
        }
        return first + [marker] + rest

    return first + rest


# ---------------------------------------------------------------------------
# Query classification (reused from local engine, slightly adjusted)
# ---------------------------------------------------------------------------

_ACTION_KEYWORDS = {
    "run", "execute", "fix", "edit", "delete", "create", "write",
    "add", "remove", "change", "modify", "refactor", "optimize",
    "install", "apply", "patch", "generate", "autofix", "implement",
    "make", "do", "build", "test", "setup", "configure", "deploy",
    "scaffold", "migrate", "upgrade", "debug", "resolve",
}

_ANALYSIS_KEYWORDS = {
    "what", "how", "why", "when", "where", "explain", "describe",
    "analyze", "check", "find", "search", "list", "show", "tell",
    "count", "summarize", "review", "understand", "compare",
}


def _classify_query(user_input: str) -> str:
    """Classify user query into: scaffolding, large_scaffolding, test_fix, debugging, analysis, action."""
    lower = user_input.lower().strip()

    # --- Scaffolding detection (before analysis, so "build X" routes correctly) ---
    if _is_scaffolding_query(lower):
        if _is_large_scaffolding_query(lower, user_input):
            return "large_scaffolding"
        return "scaffolding"

    # --- Test-fix detection ---
    if _is_test_fix_query(lower):
        return "test_fix"

    # --- Debugging detection ---
    if _is_debugging_query(lower):
        return "debugging"

    # Question phrases that strongly indicate analysis
    question_phrases = (
        "how to ", "how do ", "how can ", "how does ", "how should ",
        "what is ", "what are ", "what does ", "what do ",
        "why is ", "why does ", "why do ",
        "explain ", "describe ", "tell me about ",
        "are there ", "is there ", "is this ", "is it ",
        "does this ", "does it ", "do you ", "do i ",
        "can you explain", "can you describe", "can you show",
        "can you list", "can you tell",
        "where is ", "where are ", "where does ",
        "when is ", "when does ", "when should ",
        "which ", "who ",
        "show me ", "list ", "summarize ", "summarise ",
        "review ", "analyze ", "analyse ",
    )
    for p in question_phrases:
        if lower.startswith(p):
            # But override for imperative in disguise
            imperative = ("can you fix", "can you create", "can you write", "can you run",
                          "could you fix", "could you create", "would you fix")
            if any(i in lower for i in imperative):
                return "action"
            return "analysis"

    # Question marks with no action verbs → analysis
    if "?" in lower:
        words = set(lower.split())
        if not words & _ACTION_KEYWORDS:
            return "analysis"

    # Explicit action verbs at the start → action
    first_word = lower.split()[0] if lower.split() else ""
    if first_word in _ACTION_KEYWORDS:
        return "action"

    # Check if the sentence looks like a question even without ?
    if first_word in ("is", "are", "does", "do", "has", "have", "was", "were",
                      "will", "would", "could", "should", "can", "which", "who"):
        if not any(a in lower for a in ("fix", "create", "write", "edit", "change", "modify", "add", "remove", "delete")):
            return "analysis"

    # Default to action for imperative statements
    return "action"


def _is_scaffolding_query(lower: str) -> bool:
    """Detect queries that ask to build/create a new application or project."""
    scaffolding_phrases = (
        "build a ", "build an ", "create a ", "create an ",
        "make a ", "make an ", "scaffold ", "generate a ", "generate an ",
        "setup a ", "setup an ", "set up a ", "set up an ",
        "start a ", "start an ", "initialize a ", "init a ",
        "bootstrap a ", "bootstrap an ", "new project", "new app",
        "build me ", "create me ", "make me ",
    )
    app_keywords = {
        "app", "application", "project", "website", "webapp", "web app",
        "api", "service", "microservice", "backend", "frontend",
        "fullstack", "full-stack", "full stack", "dashboard",
        "platform", "system", "tool", "cli", "bot", "server",
        "portal", "saas", "crud", "rest api", "graphql",
    }
    if any(lower.startswith(p) for p in scaffolding_phrases):
        if any(kw in lower for kw in app_keywords):
            return True
        # Even without app keyword, if it's a create/build command it's scaffolding
        return True
    # Also detect "i want a ...", "i need a ..."
    if any(lower.startswith(p) for p in ("i want ", "i need ", "i'd like ", "give me ")):
        if any(kw in lower for kw in app_keywords):
            return True
    return False


def _is_large_scaffolding_query(lower: str, original: str) -> bool:
    """Detect queries that request a large, complex multi-feature application."""
    # Long prompts (> 200 chars with requirements) suggest complex projects
    if len(original) > 200:
        comprehensiveness = (
            "full", "complete", "comprehensive", "production",
            "enterprise", "real-world", "professional", "robust",
            "all features", "every feature", "fully implemented",
            "full-stack", "fullstack", "multiple", "complex",
        )
        if any(kw in lower for kw in comprehensiveness):
            return True

    # Multiple features listed (commas, numbered lists, "and" chains)
    comma_count = lower.count(",")
    numbered = sum(1 for i in range(1, 20) if f"{i})" in lower or f"{i}." in lower)
    if comma_count >= 4 or numbered >= 3:
        return True

    # Specific multi-stack indicators
    multi_stack = (
        "frontend and backend", "front-end and back-end",
        "react and ", "vue and ", "angular and ",
        "database", "authentication", "authorization",
        "test suite", "testing", "deployment",
    )
    stack_matches = sum(1 for kw in multi_stack if kw in lower)
    if stack_matches >= 2:
        return True

    return False


def _is_test_fix_query(lower: str) -> bool:
    """Detect queries about fixing test failures."""
    test_words = ("test", "tests", "pytest", "unittest", "spec", "specs")
    fix_words = ("fix", "repair", "resolve", "pass", "passing", "failing", "failed", "failure", "broken")
    has_test = any(w in lower for w in test_words)
    has_fix = any(w in lower for w in fix_words)
    if has_test and has_fix:
        return True
    # Direct patterns
    direct = ("run tests and fix", "make tests pass", "fix failing test", "fix the test")
    return any(p in lower for p in direct)


def _is_debugging_query(lower: str) -> bool:
    """Detect queries about debugging/fixing bugs or errors."""
    debug_phrases = (
        "bug", "crash", "error", "exception", "traceback",
        "not working", "doesn't work", "broken", "issue with",
        "problem with", "debug", "investigate",
    )
    has_debug = any(w in lower for w in debug_phrases)
    if not has_debug:
        return False
    # But not if it's a scaffolding query
    scaffolding_words = ("build", "create", "make", "scaffold", "generate", "new project")
    if any(w in lower for w in scaffolding_words):
        return False
    return True


# ---------------------------------------------------------------------------
# CloudChatEngine
# ---------------------------------------------------------------------------


class CloudChatEngine:
    """Interactive chat engine backed by the cloud Gemini API + tools."""

    _MAX_TOOL_ROUNDS = 200
    _MAX_HISTORY_MESSAGES = 100  # much more than local engine's 16

    def __init__(
        self,
        config: LocalForgeConfig,
        client: CloudClient,
        repo_path: Path,
        credential_store: Any = None,
    ) -> None:
        self.config = config
        self.client = client
        self.repo_path = repo_path
        self._credential_store = credential_store

        self.session = CloudChatSession(
            session_id=hashlib.sha256(str(repo_path).encode()).hexdigest()[:12],
            repo_path=str(repo_path),
        )
        self.tools = ToolExecutor(repo_path)
        self._repo_map_cache: str = ""
        self._token_count: int = 0
        self._tool_calls_count: int = 0
        self._rounds_count: int = 0

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def _get_session_path(self) -> Path:
        return self.repo_path / ".localforge" / "cloud_chat_history.json"

    def load_session(self) -> bool:
        path = self._get_session_path()
        if path.is_file():
            try:
                self.session = CloudChatSession.load(path)
                if self.session.conversation_id:
                    self.client.conversation_id = self.session.conversation_id
                    self.client._api_messages = list(self.session.api_messages)
                return True
            except Exception:
                logger.debug("Could not load cloud session", exc_info=True)
        return False

    def save_session(self) -> None:
        self.session.conversation_id = self.client.conversation_id
        self.session.api_messages = list(self.client._api_messages)
        self.session.save(self._get_session_path())

    # ------------------------------------------------------------------
    # Focus path helpers (mirrors local engine)
    # ------------------------------------------------------------------

    def _matches_focus(self, rel_path: str) -> bool:
        if not self.session.has_focus():
            return True
        normalised = rel_path.replace("\\", "/").strip("/")
        for fp in self.session.focus_paths:
            if normalised == fp or normalised.startswith(fp.rstrip("/") + "/"):
                return True
        return False

    def _sync_focus_to_tools(self) -> None:
        self.tools.focus_paths = list(self.session.focus_paths)

    def _invalidate_repo_map(self) -> None:
        self._repo_map_cache = ""

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def _ensure_index(self) -> None:
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
        """Build an expanded repo map — cloud model can handle much more."""
        if self._repo_map_cache:
            return self._repo_map_cache

        _skip_dirs = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".localforge", ".tox", ".mypy_cache",
            ".pytest_cache", ".eggs", "*.egg-info",
        }

        has_focus = self.session.has_focus()
        if has_focus:
            lines = [
                "PROJECT STRUCTURE (focused on: "
                + ", ".join(self.session.focus_paths) + "):"
            ]
        else:
            lines = ["PROJECT STRUCTURE:"]

        file_count = 0
        max_files = 500  # much higher than local engine's 200

        for dirpath, dirnames, filenames in os.walk(self.repo_path):
            dirnames[:] = [
                d for d in sorted(dirnames)
                if d not in _skip_dirs and not d.startswith(".")
            ]
            try:
                rel = Path(dirpath).relative_to(self.repo_path)
            except ValueError:
                continue

            rel_posix = rel.as_posix() if rel.parts else ""

            if has_focus and rel_posix:
                if not self._matches_focus(rel_posix):
                    is_ancestor = any(
                        fp.startswith(rel_posix.rstrip("/") + "/")
                        for fp in self.session.focus_paths
                    )
                    if not is_ancestor:
                        dirnames.clear()
                        continue

            depth = len(rel.parts)
            if depth > 6:
                continue

            indent = "  " * depth
            dir_name = rel.name if depth > 0 else str(self.repo_path.name)
            lines.append(f"{indent}{dir_name}/")

            for fname in sorted(filenames):
                if fname.startswith(".") or fname.endswith((".pyc", ".pyo")):
                    continue
                if has_focus:
                    file_rel = f"{rel_posix}/{fname}" if rel_posix else fname
                    if not self._matches_focus(file_rel):
                        continue
                file_count += 1
                if file_count > max_files:
                    lines.append(f"{indent}  ... (more files)")
                    break
                lines.append(f"{indent}  {fname}")

            if file_count > max_files:
                break

        # Add symbols from index
        try:
            from localforge.index import IndexSearcher
            db_path = self.repo_path / self.config.index_db_path
            if db_path.is_file():
                searcher = IndexSearcher(db_path)
                try:
                    conn = searcher._get_conn()
                    rows = conn.execute(
                        """
                        SELECT s.name, s.kind, s.line, f.relative_path
                          FROM symbols s
                          JOIN files f ON f.id = s.file_id
                         WHERE s.scope = 'module' OR s.kind IN ('class', 'interface')
                         ORDER BY f.relative_path, s.line
                         LIMIT 300
                        """
                    ).fetchall()
                    if rows:
                        lines.append("\nKEY DEFINITIONS:")
                        by_file: dict[str, list[str]] = {}
                        for row in rows:
                            fp = row["relative_path"]
                            if has_focus and not self._matches_focus(fp):
                                continue
                            entry = f"  {row['kind']}: {row['name']} (L{row['line']})"
                            by_file.setdefault(fp, []).append(entry)
                        for fp, entries in sorted(by_file.items()):
                            lines.append(f"  {fp}")
                            lines.extend(entries[:15])
                finally:
                    searcher.close()
        except Exception:
            logger.debug("Could not load symbols for repo map", exc_info=True)

        self._repo_map_cache = "\n".join(lines)
        return self._repo_map_cache

    def _build_context(self, query: str, limit: int = 20) -> str:
        """Retrieve codebase context — higher limit for cloud model."""
        try:
            from localforge.index import IndexSearcher, RepositoryIndexer
            from localforge.retrieval import ContextRetriever

            db_path = self.repo_path / self.config.index_db_path
            if not db_path.is_file():
                self._ensure_index()
                if not db_path.is_file():
                    return ""

            indexer = RepositoryIndexer(self.repo_path, db_path, self.config)
            searcher = IndexSearcher(db_path)
            try:
                retriever = ContextRetriever(indexer, searcher, self.config)
                focus = self.session.focus_paths if self.session.has_focus() else None
                result = retriever.retrieve(query, limit=max(1, limit), focus_paths=focus)
                if not result.chunks:
                    return ""
                parts = []
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

    def _build_focus_context(self, max_chars: int = 60_000) -> str:
        """Read focused files — bigger budget for cloud model."""
        if not self.session.has_focus():
            return ""

        _skip_dirs = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".localforge",
        }
        _skip_ext = {".pyc", ".pyo", ".exe", ".dll", ".so", ".bin", ".png",
                     ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2"}

        files_to_read: list[Path] = []
        for fp in self.session.focus_paths:
            full = self.repo_path / fp
            if full.is_file():
                files_to_read.append(full)
            elif full.is_dir():
                for child in sorted(full.rglob("*")):
                    if not child.is_file():
                        continue
                    if any(part in _skip_dirs for part in child.parts):
                        continue
                    if child.suffix.lower() in _skip_ext:
                        continue
                    if child.stat().st_size > 1_000_000:
                        continue
                    files_to_read.append(child)

        if not files_to_read:
            return ""

        files_to_read.sort(key=lambda p: p.stat().st_size)

        parts: list[str] = []
        chars_used = 0

        for fpath in files_to_read:
            try:
                rel = fpath.relative_to(self.repo_path).as_posix()
            except ValueError:
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            remaining = max_chars - chars_used
            if remaining <= 200:
                break
            if len(content) > remaining:
                content = content[:remaining] + "\n... [truncated]"

            line_count = content.count("\n") + 1
            parts.append(f"--- {rel} (L1-L{line_count}) ---\n{content}")
            chars_used += len(content) + len(rel) + 30

        if not parts:
            return ""

        return "=== FOCUSED FILES ===\n" + "\n\n".join(parts) + "\n=== END FOCUSED FILES ==="

    def _recent_messages(self, max_messages: int | None = None) -> list[dict[str, Any]]:
        if max_messages is None:
            max_messages = self._MAX_HISTORY_MESSAGES
        msgs = self.session.messages
        if len(msgs) > max_messages:
            msgs = msgs[-max_messages:]
        result: list[dict[str, Any]] = []
        for m in msgs:
            d: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.role == "assistant" and m.thinking:
                d["thinking"] = m.thinking
            result.append(d)
        return result

    def _append_project_rules(self, system_parts: list[str]) -> None:
        rules_path = self.repo_path / ".localforge" / "rules.md"
        if rules_path.is_file():
            try:
                rules = rules_path.read_text(encoding="utf-8").strip()
                lines = [ln for ln in rules.splitlines() if ln.strip() and not ln.strip().startswith("#")]
                if lines:
                    system_parts[0] += f"\n\nPROJECT RULES:\n{rules}"
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Auth re-prompt
    # ------------------------------------------------------------------

    async def _handle_auth_expired(self) -> bool:
        """Called when the API returns 401/403. Returns True if re-auth succeeded."""
        if not self._credential_store:
            console.print(
                "[bold red]Session expired.[/bold red] Re-run localforge cloud-chat to re-authenticate."
            )
            return False
        console.print(
            "\n[bold yellow]Authentication refresh required.[/bold yellow]\n"
            "[dim]This can happen when cookies expire or when the remote conversation state becomes invalid.[/dim]"
        )
        try:
            parsed = self._credential_store.prompt_for_headers()
            # Rebuild client headers and connection
            with contextlib.suppress(Exception):
                await self.client._client.aclose()
            self.client._headers.update(parsed.get("headers", {}))
            self.client._client = self.client._new_httpx_client()
            self._clear_remote_session_state(reason="reauthenticated")
            return True
        except (ValueError, KeyboardInterrupt):
            console.print("[red]Re-authentication cancelled.[/red]")
            return False

    def _clear_remote_session_state(self, *, reason: str = "") -> None:
        """Clear persisted cloud conversation state while keeping visible chat history."""
        self.client.reset_conversation()
        self.session.conversation_id = ""
        self.session.api_messages = []
        self.save_session()
        if reason:
            logger.info("Cleared remote cloud conversation state: %s", reason)

    async def _recover_from_auth_error(self, *, allow_session_reset: bool = True) -> bool:
        """Try session-state recovery before prompting for new auth headers."""
        has_remote_state = bool(
            self.client.conversation_id
            or self.client._api_messages
            or self.session.conversation_id
            or self.session.api_messages
        )
        if allow_session_reset and has_remote_state:
            console.print(
                "\n[yellow]Remote conversation state expired. Resetting session and retrying once…[/yellow]"
            )
            self._clear_remote_session_state(reason="auth error recovery")
            return True
        return await self._handle_auth_expired()

    # ------------------------------------------------------------------
    # Send message
    # ------------------------------------------------------------------

    async def send_message(self, user_input: str) -> str:
        self.session.add_user_message(user_input)

        query_type = _classify_query(user_input)
        logger.info("Query classified as: %s", query_type)

        if query_type == "analysis":
            return await self._handle_analysis_query(user_input)
        return await self._handle_action_query(user_input, query_type=query_type)

    async def _handle_analysis_query(
        self,
        user_input: str,
        *,
        allow_session_reset: bool = True,
        _vpn_retried: bool = False,
    ) -> str:
        """Direct answer without tool loop — fast for questions."""
        has_focus = self.session.has_focus()
        repo_map = self._build_repo_map()
        context = self._build_context(user_input, limit=10)
        focus_context = self._build_focus_context() if has_focus else ""

        system = CLOUD_ANALYSIS_PROMPT
        # Inject working directory so the model knows the project context
        repo_abs = str(self.repo_path).replace("\\", "/")
        system += f"\n\nWorking directory: {repo_abs}\nProject: {self.repo_path.name}"
        if repo_map:
            system += f"\n\n{repo_map}"
        if focus_context:
            system += f"\n\n{focus_context}"
        if context:
            system += f"\n\nCODE CONTEXT:\n{context}"

        self._append_project_rules(system_parts := [system])
        system = system_parts[0]

        working_messages = self._recent_messages(max_messages=50)

        spinner = Live(
            Spinner("dots", text="[bold gold1]cloud[/bold gold1] analyzing…"),
            refresh_per_second=10, transient=True, console=console,
        )
        spinner.start()

        parts: list[str] = []
        thinking_parts: list[str] = []
        first_token = False
        in_thinking = False

        try:
            try:
                stream = self.client.chat_stream_tokens(
                    working_messages, system=system, temperature=0.3,
                )
                async for token in stream:
                    if token.startswith(THINKING_TOKEN_PREFIX):
                        thinking = token[len(THINKING_TOKEN_PREFIX):]
                        if thinking:
                            thinking_parts.append(thinking)
                            if not in_thinking:
                                if not first_token:
                                    first_token = True
                                    spinner.stop()
                                console.print("[dim italic]thinking: ", end="")
                                in_thinking = True
                            console.print(f"[dim italic]{thinking}[/]", end="", highlight=False)
                        continue

                    if not first_token:
                        first_token = True
                        spinner.stop()
                    if in_thinking:
                        console.print()  # newline after thinking block
                        in_thinking = False
                        console.print("[bold green]cloud[/bold green] ", end="")

                    if not parts and not in_thinking:
                        console.print("[bold green]cloud[/bold green] ", end="")

                    parts.append(token)
                    console.print(token, end="", highlight=False)

                if not first_token:
                    spinner.stop()
                if parts:
                    console.print()

            except AuthExpiredError:
                spinner.stop()
                if await self._recover_from_auth_error(allow_session_reset=allow_session_reset):
                    return await self._handle_analysis_query(user_input, allow_session_reset=False, _vpn_retried=_vpn_retried)
                return "Session expired. Please re-authenticate."
            except VPNError as exc:
                spinner.stop()
                if not _vpn_retried:
                    console.print(
                        "\n[yellow]Connection lost — recreating client and retrying…[/yellow]"
                    )
                    with contextlib.suppress(Exception):
                        await self.client._client.aclose()
                    self.client._client = self.client._new_httpx_client()
                    await asyncio.sleep(3)
                    return await self._handle_analysis_query(
                        user_input,
                        allow_session_reset=allow_session_reset,
                        _vpn_retried=True,
                    )
                err_msg = str(exc).replace("[", "\\[")
                console.print(f"\n[bold red]VPN Error:[/bold red] {err_msg}")
                return str(exc)

        finally:
            with contextlib.suppress(Exception):
                spinner.stop()

        response = "".join(parts)
        self._token_count += len(parts)
        self.session.add_assistant_message(response, thinking="".join(thinking_parts))
        self.save_session()
        return response

    async def _handle_action_query(
        self,
        user_input: str,
        *,
        query_type: str = "action",
        allow_session_reset: bool = True,
    ) -> str:
        """Tool-calling loop for action queries.

        *query_type* can be: action, scaffolding, large_scaffolding,
        test_fix, debugging.  Each type selects a tailored system prompt
        and execution parameters.
        """
        has_focus = self.session.has_focus()

        # --- Select system prompt based on query type ---
        is_scaffolding = query_type in ("scaffolding", "large_scaffolding")
        is_test_fix = query_type == "test_fix"
        is_debugging = query_type == "debugging"

        if query_type == "large_scaffolding":
            base_prompt = CLOUD_LARGE_SCAFFOLDING_PROMPT
            max_rounds = 120  # Very generous for large projects
            console.print("[dim]Mode: large project scaffolding (extended rounds)[/dim]")
        elif query_type == "scaffolding":
            base_prompt = CLOUD_SCAFFOLDING_PROMPT
            max_rounds = 80
            console.print("[dim]Mode: project scaffolding[/dim]")
        elif is_test_fix:
            base_prompt = CLOUD_TEST_FIX_PROMPT
            max_rounds = 40
            console.print("[dim]Mode: test-fix[/dim]")
        elif is_debugging:
            base_prompt = CLOUD_DEBUGGING_PROMPT
            max_rounds = 60
            console.print("[dim]Mode: debugging[/dim]")
        else:
            base_prompt = CLOUD_SYSTEM_PROMPT
            max_rounds = self._MAX_TOOL_ROUNDS

        system = base_prompt + "\n\n" + CLOUD_TOOL_PROMPT

        # Inject working directory context — critical for the model to know
        # where it is and how to construct paths for external projects
        repo_name = self.repo_path.name
        repo_abs = str(self.repo_path).replace("\\", "/")
        repo_parent = str(self.repo_path.parent).replace("\\", "/")
        system += (
            f"\n\n═══════════════════ WORKING DIRECTORY ═══════════════════\n"
            f"Current working directory: {repo_abs}\n"
            f"Project name: {repo_name}\n"
            f"Parent directory: {repo_parent}\n"
            f"All tool file paths are relative to: {repo_abs}\n"
            f"To create a new project OUTSIDE this repo, use base_path like \"../<project_name>\" "
            f"(which resolves to {repo_parent}/<project_name>).\n"
            f"To run commands in an external project, use run_command with "
            f"cwd=\"../<project_name>\".\n"
            f"To read/edit files in an external project, use paths like "
            f"\"../<project_name>/filename.py\"."
        )

        # Add repo map and context (skip for scaffolding — new project doesn't need it)
        if not is_scaffolding:
            repo_map = self._build_repo_map()
            context = self._build_context(user_input, limit=15)
            if repo_map:
                system += f"\n\n{repo_map}"
            if context:
                system += f"\n\nRELEVANT CODE CONTEXT:\n{context}"

        if has_focus:
            focus_context = self._build_focus_context()
            if focus_context:
                system += f"\n\n{focus_context}"
            focus_list = ", ".join(self.session.focus_paths)
            system += (
                f"\n\nFOCUS SCOPE: Working with: {focus_list}\n"
                "File content is provided above. Edit directly."
            )

        self._append_project_rules(system_parts := [system])
        system = system_parts[0]

        working_messages = self._recent_messages(max_messages=self._MAX_HISTORY_MESSAGES)

        final_response = ""
        tool_call_history: dict[str, int] = {}
        consecutive_no_tool = 0
        task_verified = False
        successful_actions_count = 0  # Track progress for scaffolding
        files_created_count = 0
        has_installed_deps = False
        has_run_tests = False
        can_reset_session = allow_session_reset
        vpn_retries_left = 2  # auto-retry VPNError this many times

        for _round in range(max_rounds):
            # --- Spinner ---
            if _round == 0:
                prompt_k = (len(system) + sum(len(m.get("content", "")) for m in working_messages)) // 1000
                spinner_text = f"[bold gold1]cloud[/bold gold1] thinking… [dim](~{prompt_k}K chars, {query_type})[/dim]"
            else:
                ctx_k = sum(len(m.get("content", "")) for m in working_messages) // 1000
                spinner_text = f"[bold gold1]cloud[/bold gold1] working… (step {_round + 1}, ~{ctx_k}K ctx)"

            spinner = Live(
                Spinner("dots", text=spinner_text),
                refresh_per_second=10, transient=True, console=console,
            )
            spinner.start()

            # --- Stream from model ---
            parts: list[str] = []
            thinking_parts: list[str] = []
            first_token = False
            console_prefix_printed = False
            in_thinking = False

            try:
                try:
                    stream = self.client.chat_stream_tokens(
                        working_messages, system=system, temperature=0.4,
                    )
                    async for token in stream:
                        if token.startswith(THINKING_TOKEN_PREFIX):
                            thk = token[len(THINKING_TOKEN_PREFIX):]
                            if thk:
                                thinking_parts.append(thk)
                                if not in_thinking:
                                    if not first_token:
                                        first_token = True
                                        spinner.stop()
                                    console.print("[dim italic]thinking: ", end="")
                                    in_thinking = True
                                console.print(f"[dim italic]{thk}[/]", end="", highlight=False)
                            continue

                        if not first_token:
                            first_token = True
                            spinner.stop()
                        if in_thinking:
                            console.print()  # newline after thinking block
                            in_thinking = False

                        if not console_prefix_printed:
                            console.print("[bold green]cloud[/bold green] ", end="")
                            console_prefix_printed = True

                        parts.append(token)
                        console.print(token, end="", highlight=False)

                    if not first_token:
                        spinner.stop()
                    if console_prefix_printed:
                        console.print()

                except AuthExpiredError:
                    spinner.stop()
                    if await self._recover_from_auth_error(allow_session_reset=can_reset_session):
                        can_reset_session = False
                        continue  # retry this round
                    self.session.add_assistant_message("Session expired.")
                    self.save_session()
                    return "Session expired."
                except VPNError as exc:
                    spinner.stop()
                    if vpn_retries_left > 0:
                        vpn_retries_left -= 1
                        console.print(
                            "\n[yellow]Connection lost — recreating client and retrying…[/yellow]"
                        )
                        with contextlib.suppress(Exception):
                            await self.client._client.aclose()
                        self.client._client = self.client._new_httpx_client()
                        await asyncio.sleep(3)
                        continue  # retry this round
                    err_msg = str(exc).replace("[", "\\[")
                    console.print(f"\n[bold red]VPN Error:[/bold red] {err_msg}")
                    self.session.add_assistant_message(str(exc))
                    self.save_session()
                    return str(exc)
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        spinner.stop()
                    partial = "".join(parts)
                    if partial.strip():
                        console.print()
                        console.print("  [yellow]⚠ Connection interrupted — using partial response[/yellow]")
                    else:
                        err_msg = str(exc).replace("[", "\\[")  # escape Rich markup
                        console.print(f"\n[bold red]Error:[/bold red] {err_msg}")
                        self.session.add_assistant_message(f"Error: {exc}")
                        self.save_session()
                        return f"Error: {exc}"

            finally:
                with contextlib.suppress(Exception):
                    spinner.stop()

            content = "".join(parts)
            self._token_count += len(parts)
            self._rounds_count += 1

            # --- Extract tool calls ---
            tool_calls: list[dict[str, Any]] = []
            _, xml_calls = extract_all_tool_calls(content)
            tool_calls = xml_calls

            if not tool_calls and not task_verified:
                _, json_calls = extract_json_tool_calls(content)
                tool_calls = json_calls

            # --- Execute tool calls ---
            if tool_calls:
                consecutive_no_tool = 0

                # Cloud model can batch many calls — allow up to 20 per round
                if len(tool_calls) > 20:
                    logger.info("Capping %d tool calls to 20", len(tool_calls))
                    tool_calls = tool_calls[:20]

                all_results: list[tuple[str, str]] = []

                for i, tc in enumerate(tool_calls, 1):
                    tool_name = tc.get("tool", "unknown")
                    tool_args = tc.get("args", {})

                    validation_err = validate_tool_call(tc)
                    if validation_err is not None:
                        # Defensive: validate_tool_call must return None for valid calls
                        # or a string error message. Log unexpected types.
                        if not isinstance(validation_err, str):
                            logger.warning(
                                "validate_tool_call returned non-string: %r for %s",
                                validation_err, tool_name,
                            )
                            validation_err = str(validation_err)
                        console.print(f"  [dim]⚡ Tool {tool_name} ✗ {validation_err}[/dim]")
                        all_results.append((tool_name, f"Error: {validation_err}"))
                        continue

                    call_hash = hash_tool_call(tool_name, tool_args)
                    tool_call_history[call_hash] = tool_call_history.get(call_hash, 0) + 1

                    if tool_call_history[call_hash] > 3:
                        redirect = (
                            f"BLOCKED: {tool_name} called {tool_call_history[call_hash]}x with same args. "
                            "Try a different approach."
                        )
                        if tool_name == "edit_file":
                            redirect += " Use edit_lines with line numbers instead."
                        elif tool_name == "search_code":
                            redirect += " Use grep_codebase instead, or try a different search term."
                        console.print(f"  [yellow]⚠ Skipping repeated: {tool_name}[/yellow]")
                        all_results.append((tool_name, f"Error: {redirect}"))
                        continue

                    label = f"[{i}/{len(tool_calls)}]" if len(tool_calls) > 1 else ""
                    console.print(f"  [dim]⚡ Tool {label} {tool_name}[/dim]", end="")

                    # Print arg preview
                    if tool_name in ("read_file", "write_file", "edit_file", "edit_lines", "apply_diff"):
                        p = tool_args.get("path", "")
                        if p:
                            console.print(f" [dim]{p}[/dim]", end="")
                    elif tool_name == "run_command":
                        cmd = tool_args.get("command", "")
                        cwd = tool_args.get("cwd", "")
                        if cmd:
                            console.print(f" [dim]{cmd[:80]}[/dim]", end="")
                        if cwd:
                            console.print(f" [dim](in {cwd})[/dim]", end="")
                    elif tool_name in ("search_code", "grep_codebase"):
                        pat = tool_args.get("pattern", "")
                        if pat:
                            console.print(f" [dim]{pat[:60]}[/dim]", end="")
                    elif tool_name == "create_project":
                        bp = tool_args.get("base_path", "")
                        nfiles = len(tool_args.get("files", {}))
                        console.print(f" [dim]{bp} ({nfiles} files)[/dim]", end="")
                    elif tool_name == "create_directory":
                        p = tool_args.get("path", "")
                        if p:
                            console.print(f" [dim]{p}[/dim]", end="")
                    console.print()

                    start_t = time.monotonic()
                    result = self.tools.execute(tool_name, tool_args)
                    elapsed = time.monotonic() - start_t

                    self._tool_calls_count += 1

                    # If search_code found nothing, suggest grep_codebase
                    if tool_name == "search_code" and result in ("(no matches)", "No matches found"):
                        pattern = tool_args.get("pattern", "")
                        result += (
                            f"\nHint: search_code found no results for '{pattern}'. "
                            "Try grep_codebase instead — it searches all files reliably. "
                            "Or try a shorter/different search term."
                        )

                    # Track progress for scaffolding
                    if not result.startswith("Error:"):
                        successful_actions_count += 1
                        if tool_name == "create_project":
                            nfiles = len(tool_args.get("files", {}))
                            files_created_count += nfiles
                            console.print(f"  [green]  📁 Created {nfiles} files ({files_created_count} total)[/green]")
                        elif tool_name == "write_file":
                            files_created_count += 1
                        elif tool_name == "run_command":
                            cmd_lower = tool_args.get("command", "").lower()
                            if any(pkg in cmd_lower for pkg in ("pip install", "npm install", "go mod", "cargo build", "mvn install", "gradle", "yarn")):
                                has_installed_deps = True
                            if any(t in cmd_lower for t in ("pytest", "npm test", "go test", "cargo test", "mvn test", "jest", "vitest", "mocha")):
                                has_run_tests = True

                    # Reset dedup for run/verify after edits
                    edit_tools = {"edit_file", "edit_lines", "write_file", "batch_edit", "apply_diff"}
                    if tool_name in edit_tools and not result.startswith("Error:"):
                        stale = [h for h, c in tool_call_history.items() if c > 1]
                        for h in stale:
                            tool_call_history[h] = 1

                    # Check for task-verified-clean
                    _clean = ("all checks passed", "no issues", "found 0 error", "0 error(s)")
                    result_lower = result.lower()
                    if tool_name in ("verify_changes", "run_command"):
                        if any(m in result_lower for m in _clean):
                            task_verified = True
                        elif " passed" in result_lower and "(exit code:" not in result_lower:
                            task_verified = True

                    preview = result[:150].replace("\n", " ")
                    if len(result) > 150:
                        preview += "…"
                    icon = "✓" if not result.startswith("Error:") else "✗"
                    console.print(f"  [dim]   {icon} ({elapsed:.1f}s) {preview}[/dim]")

                    all_results.append((tool_name, result))

                console.print()

                # Feed results back to the model
                working_messages.append({"role": "assistant", "content": content})

                # Compress create_project results to save context space
                processed_results: list[tuple[str, str]] = []
                for name, res in all_results:
                    if name == "create_project" and len(res) > 3000:
                        processed_results.append((name, _compress_create_project_result(res)))
                    else:
                        processed_results.append((name, res))

                result_text = "\n\n".join(
                    f"Tool result for {name}:\n```\n{_truncate_tool_result(res)}\n```"
                    for name, res in processed_results
                )

                # --- Scaffolding-specific nudges ---
                scaffolding_nudge = ""
                if is_scaffolding:
                    if files_created_count > 0 and not has_installed_deps:
                        scaffolding_nudge = (
                            "\n\nIMPORTANT: Files have been created. Now you MUST:\n"
                            "1. cd into the project directory using run_command with cwd\n"
                            "2. Install dependencies (pip install -r requirements.txt, npm install, etc.)\n"
                            "3. Run the test suite to verify everything works\n"
                            "Do NOT stop until tests pass."
                        )
                    elif has_installed_deps and not has_run_tests:
                        scaffolding_nudge = (
                            "\n\nDependencies installed. Now RUN THE TESTS to verify everything works. "
                            "Use run_command with the appropriate test command and cwd set to the project directory."
                        )
                    elif has_run_tests and not task_verified and " failed" in result_lower:
                        scaffolding_nudge = (
                            "\n\nTests have FAILURES. Read the error messages carefully, "
                            "fix the failing code, and re-run tests. Keep iterating until ALL tests pass."
                        )

                working_messages.append({"role": "user", "content": result_text + scaffolding_nudge})

                # Prune old messages if context is getting too large
                working_messages = _prune_working_messages(working_messages)

                final_response = content
                continue

            # --- No tool calls: model gave a final text response ---
            consecutive_no_tool += 1
            final_response = content

            # Check if the model is being lazy (giving instructions instead of acting)
            if consecutive_no_tool == 1 and _round == 0 and not task_verified:
                lower = content.lower()
                lazy_phrases = ("you can run", "you should run", "follow these steps",
                                "here are the steps", "i recommend", "i suggest",
                                "you'll need to", "you need to", "please run",
                                "next steps:", "to get started:")
                is_lazy = sum(1 for p in lazy_phrases if p in lower) >= 2
                if is_lazy:
                    working_messages.append({"role": "assistant", "content": content})
                    working_messages.append({
                        "role": "user",
                        "content": "Do NOT give instructions. YOU must execute using tools. Act now.",
                    })
                    continue

            # Scaffolding early-exit prevention: if we haven't verified, nudge to continue
            if is_scaffolding and not task_verified:
                if files_created_count > 0 and not has_run_tests:
                    working_messages.append({"role": "assistant", "content": content})
                    nudge = (
                        "The project creation is NOT complete yet. You MUST:\n"
                    )
                    if not has_installed_deps:
                        nudge += "1. Install dependencies using run_command (with cwd set to the project directory)\n"
                    nudge += (
                        f"{'2' if not has_installed_deps else '1'}. Run the test suite to verify everything works\n"
                        "Use tools now. Do NOT stop without running tests."
                    )
                    working_messages.append({"role": "user", "content": nudge})
                    continue
                if files_created_count == 0 and successful_actions_count < 2:
                    working_messages.append({"role": "assistant", "content": content})
                    working_messages.append({
                        "role": "user",
                        "content": (
                            "You haven't created any files yet. Use write_file to create "
                            "the project files. Act now with tools."
                        ),
                    })
                    continue

            break  # Model gave a real response without tool calls

        # Save final state
        self.session.add_assistant_message(final_response, thinking="".join(thinking_parts) if 'thinking_parts' in dir() else "")
        self.save_session()

        # Print session stats
        if is_scaffolding and files_created_count > 0:
            status_parts = [f"[dim]📊 {files_created_count} files created"]
            if has_installed_deps:
                status_parts.append("deps installed")
            if has_run_tests:
                status_parts.append("tests run")
            if task_verified:
                status_parts.append("✅ verified")
            console.print(", ".join(status_parts) + f", {_round + 1} rounds[/dim]")

        return final_response

    # ------------------------------------------------------------------
    # REPL
    # ------------------------------------------------------------------

    async def run_repl(self) -> None:
        """Run the interactive cloud chat REPL."""
        self._ensure_index()
        self._build_repo_map()

        loaded = self.load_session()
        if loaded and self.session.messages:
            n = len(self.session.messages)
            console.print(
                f"[dim]Resumed session with {n} message(s). "
                f"Type /clear to start fresh.[/dim]"
            )

        if self.session.has_focus():
            self._sync_focus_to_tools()
            self._invalidate_repo_map()
            console.print(
                f"[dim]Focus: {', '.join(self.session.focus_paths)}[/dim]"
            )

        console.print(
            Panel(
                "[bold gold1]LocalForge Cloud[/bold gold1] — autonomous coding agent\n"
                f"Model: [bold cyan]{self.client.model}[/bold cyan] (cloud)  "
                f"Context: [cyan]128K tokens[/cyan]  "
                f"Repo: [cyan]{self.repo_path}[/cyan]\n\n"
                "[dim]Powered by Gemini 3.1 Pro — fast, powerful, autonomous.\n"
                "I read/write/edit files, run commands, search code, and verify changes.\n"
                "Just tell me what to do.\n"
                "Commands: /add, /remove, /focus, /clear-focus, /clear, /reauth, /help, /quit[/dim]",
                border_style="gold1",
                expand=False,
            )
        )

        while True:
            try:
                if self.session.has_focus():
                    n_focus = len(self.session.focus_paths)
                    prompt = f"\n[bold gold1]you[/bold gold1] [dim]({n_focus} focused)[/dim] [bold gold1]>[/bold gold1] "
                else:
                    prompt = "\n[bold gold1]you >[/bold gold1] "
                user_input = console.input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Goodbye![/dim]")
                break

            if not user_input:
                continue

            if user_input.startswith("/"):
                if await self._handle_command(user_input):
                    continue
                else:
                    break

            await self.send_message(user_input)

        self.save_session()

    async def _handle_command(self, cmd: str) -> bool:
        """Handle slash command. Returns False to exit REPL."""
        parts = cmd.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if command in ("/quit", "/exit", "/q"):
            console.print("[dim]Goodbye![/dim]")
            return False

        elif command == "/clear":
            self.session.clear()
            self.client.reset_conversation()
            self.save_session()
            console.print("[green]Chat + conversation history cleared.[/green]")

        elif command == "/reauth":
            if self._credential_store:
                self._credential_store.clear()
                try:
                    parsed = self._credential_store.prompt_for_headers()
                    with contextlib.suppress(Exception):
                        await self.client._client.aclose()
                    self.client._headers.update(parsed.get("headers", {}))
                    self.client._client = self.client._new_httpx_client()
                    self._clear_remote_session_state(reason="manual /reauth")
                    console.print("[green]Re-authenticated successfully.[/green]")
                except (ValueError, KeyboardInterrupt):
                    console.print("[red]Re-authentication cancelled.[/red]")
            else:
                console.print("[yellow]No credential store available.[/yellow]")

        elif command == "/history":
            if not self.session.messages:
                console.print("[dim]No messages yet.[/dim]")
            else:
                for i, msg in enumerate(self.session.messages, 1):
                    role_style = "gold1" if msg.role == "user" else "green"
                    label = "you" if msg.role == "user" else "cloud"
                    preview = msg.content[:120].replace("\n", " ")
                    suffix = "…" if len(msg.content) > 120 else ""
                    console.print(f"  [{role_style}]{i}. {label}:[/{role_style}] {preview}{suffix}")

        elif command == "/run":
            if not arg:
                console.print("[yellow]Usage: /run <command>[/yellow]")
            else:
                result = self.tools.execute("run_command", {"command": arg})
                console.print(Panel(result[:5000], title=f"$ {arg}", border_style="green"))

        elif command == "/read":
            if not arg:
                console.print("[yellow]Usage: /read <file path>[/yellow]")
            else:
                result = self.tools.execute("read_file", {"path": arg})
                console.print(Panel(result[:5000], title=arg, border_style="blue"))

        elif command == "/add":
            if not arg:
                console.print("[yellow]Usage: /add <file_or_folder> ...[/yellow]")
            else:
                added = 0
                for token in arg.split():
                    expanded = list(self.repo_path.glob(token))
                    if not expanded:
                        literal = self.repo_path / token
                        if literal.exists():
                            expanded = [literal]
                        else:
                            console.print(f"  [red]Not found: {token}[/red]")
                            continue
                    for p in expanded:
                        try:
                            rel = p.relative_to(self.repo_path).as_posix()
                        except ValueError:
                            continue
                        if self.session.add_focus_path(rel):
                            added += 1
                            console.print(f"  [green]+ {rel}[/green]")
                if added:
                    self._sync_focus_to_tools()
                    self._invalidate_repo_map()
                    self._build_repo_map()
                    self.save_session()

        elif command == "/remove":
            if not arg:
                console.print("[yellow]Usage: /remove <path>[/yellow]")
            else:
                removed = self.session.remove_focus_path(arg)
                if removed:
                    console.print(f"[green]Removed {removed} path(s)[/green]")
                    self._sync_focus_to_tools()
                    self._invalidate_repo_map()
                    self._build_repo_map()
                    self.save_session()
                else:
                    console.print(f"[yellow]'{arg}' not in focus.[/yellow]")

        elif command == "/focus":
            if self.session.focus_paths:
                console.print("[bold]Focused paths:[/bold]")
                for fp in self.session.focus_paths:
                    console.print(f"  [cyan]{fp}[/cyan]")
            else:
                console.print("[dim]No focus set. Use /add <path>.[/dim]")

        elif command == "/clear-focus":
            self.session.clear_focus_paths()
            self._sync_focus_to_tools()
            self._invalidate_repo_map()
            self.save_session()
            console.print("[green]Focus cleared — full codebase mode.[/green]")

        elif command == "/tokens":
            console.print(
                f"[dim]Session: ~{self._token_count} tokens, "
                f"{self._tool_calls_count} tool calls, "
                f"{self._rounds_count} rounds[/dim]"
            )

        elif command == "/help":
            console.print(
                Panel(
                    "[bold]Cloud Chat Commands[/bold]\n\n"
                    "/add <path>      Focus on specific files/folders\n"
                    "/remove <path>   Remove path from focus\n"
                    "/focus           Show current focus paths\n"
                    "/clear-focus     Reset to full codebase\n"
                    "/run <cmd>       Run a shell command directly\n"
                    "/read <file>     Read a file directly\n"
                    "/reauth          Paste new authentication headers\n"
                    "/history         Show conversation history\n"
                    "/tokens          Show token/tool usage stats\n"
                    "/clear           Clear conversation history\n"
                    "/help            Show this help\n"
                    "/quit            Exit",
                    border_style="gold1",
                    expand=False,
                )
            )

        else:
            console.print(f"[yellow]Unknown command: {command}. Type /help.[/yellow]")

        return True
