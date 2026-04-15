"""CLI entry-point for localforge."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import site
import sys
import sysconfig
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from localforge.core.config import LocalForgeConfig
    from localforge.core.models import MultiAgentState
    from localforge.index import IndexSearcher, RepositoryIndexer

# Ensure UTF-8 output on Windows to prevent UnicodeEncodeError from Rich
# rendering Unicode spinners and box-drawing characters.
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            with contextlib.suppress(Exception):
                _stream.reconfigure(encoding="utf-8", errors="replace")

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from localforge import __version__

app = typer.Typer(
    name="localforge",
    help="Local-first coding agent powered by Ollama",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _path_contains_entry(path_value: str, candidate: str) -> bool:
    candidate_norm = os.path.normcase(os.path.normpath(candidate))
    entries = [p.strip() for p in path_value.split(os.pathsep) if p.strip()]
    return any(os.path.normcase(os.path.normpath(p)) == candidate_norm for p in entries)


def _windows_user_scripts_dir() -> Path:
    # Use the active interpreter's user scheme (e.g. ...\Python313\Scripts).
    scheme = sysconfig.get_preferred_scheme("user")
    scripts = sysconfig.get_path("scripts", scheme=scheme)
    if scripts:
        return Path(scripts)
    return Path(site.getuserbase()) / "Scripts"


def ensure_windows_scripts_on_path(persist_user_path: bool = True) -> tuple[bool, str]:
    """Ensure the Windows user Scripts dir is present in PATH.

    Returns (changed, message).
    """
    if sys.platform != "win32":
        return False, "PATH setup is only needed on Windows."

    scripts_dir = str(_windows_user_scripts_dir())
    current_path = os.environ.get("PATH", "")
    changed = False

    if not _path_contains_entry(current_path, scripts_dir):
        os.environ["PATH"] = f"{current_path}{os.pathsep}{scripts_dir}" if current_path else scripts_dir
        changed = True

    if not persist_user_path:
        if changed:
            return True, f"Added to current session PATH: {scripts_dir}"
        return False, f"Scripts directory already present in current PATH: {scripts_dir}"

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        ) as key:
            try:
                user_path, reg_type = winreg.QueryValueEx(key, "Path")
                if not isinstance(user_path, str):
                    user_path = ""
            except FileNotFoundError:
                user_path, reg_type = "", winreg.REG_EXPAND_SZ

            if not _path_contains_entry(user_path, scripts_dir):
                new_user_path = (
                    f"{user_path}{os.pathsep}{scripts_dir}" if user_path else scripts_dir
                )
                winreg.SetValueEx(key, "Path", 0, reg_type, new_user_path)
                changed = True

    except OSError as exc:
        return changed, f"Could not persist PATH automatically: {exc}"

    if changed:
        return True, (
            f"Added Scripts directory to user PATH: {scripts_dir}. "
            "Open a new terminal for changes to take effect."
        )
    return False, f"Scripts directory already present in user PATH: {scripts_dir}"


def bootstrap_windows_scripts_path() -> None:
    """Best-effort PATH bootstrap for `py -m localforge` on Windows."""
    if sys.platform != "win32":
        return

    # Allow users to opt out of automatic PATH edits.
    if os.environ.get("LOCALFORGE_NO_PATH_BOOTSTRAP", "").strip() in {"1", "true", "yes"}:
        return

    with contextlib.suppress(Exception):
        ensure_windows_scripts_on_path(persist_user_path=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]localforge[/bold] {__version__}")
        raise typer.Exit()


def _resolve_repo(repo_path: Path) -> Path:
    return Path(repo_path).resolve()


def _load_config(repo_path: Path, **overrides: object) -> LocalForgeConfig:  # noqa: ANN202
    from localforge.core.config import LocalForgeConfig, load_config

    cfg = load_config(str(repo_path))
    # Ensure repo_path is set from the function argument (callers may also
    # pass it via **overrides, which would cause a duplicate-keyword error).
    overrides["repo_path"] = str(repo_path)
    cfg = LocalForgeConfig(**{**cfg.model_dump(), **overrides})
    return cfg


@app.command("setup-shell")
def setup_shell(
    persist: bool = typer.Option(
        True,
        "--persist/--session",
        help="Persist to user PATH (default) or only update current session PATH.",
    ),
) -> None:
    """Ensure LocalForge CLI is discoverable by your shell."""
    changed, message = ensure_windows_scripts_on_path(persist_user_path=persist)

    if changed:
        console.print(f"[bold green]PATH updated:[/bold green] {message}")
    else:
        console.print(f"[dim]{message}[/dim]")


def _check_ollama(config: LocalForgeConfig) -> bool:  # noqa: ANN001
    """Return True if Ollama is reachable, else print error and return False."""
    from localforge.core.ollama_client import OllamaClient

    async def _check() -> bool:
        client = OllamaClient(config)
        try:
            return await client.health_check()
        finally:
            await client.close()

    healthy = asyncio.run(_check())
    if not healthy:
        console.print(
            f"[bold red]Error:[/bold red] Cannot reach Ollama at {config.ollama_base_url}\n"
            "Make sure Ollama is running (ollama serve)."
        )
    return healthy


def _build_indexer_and_searcher(
    repo_path: Path, config: LocalForgeConfig,
) -> tuple[RepositoryIndexer, IndexSearcher]:  # noqa: ANN001
    from localforge.index import IndexSearcher, RepositoryIndexer

    db_path = repo_path / config.index_db_path
    indexer = RepositoryIndexer(repo_path, db_path, config)
    searcher = IndexSearcher(db_path)
    return indexer, searcher


def _ensure_index(repo_path: Path, config: LocalForgeConfig) -> None:  # noqa: ANN001
    """Index the repo if no index exists yet."""
    from localforge.index import RepositoryIndexer

    db_path = repo_path / config.index_db_path
    indexer = RepositoryIndexer(repo_path, db_path, config)
    if not indexer.is_initialized():
        console.print("[yellow]No index found — indexing repository first…[/yellow]")
        stats = indexer.index_repository()
        console.print(
            f"[green]Indexed {stats['indexed']} files "
            f"({stats['skipped']} skipped, {stats['errors']} errors) "
            f"in {stats['duration_seconds']}s[/green]"
        )
    indexer.close()


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        "-v",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "--debug",
        help="Enable verbose/debug logging output.",
        is_eager=True,
    ),
) -> None:
    """localforge — local-first, repo-aware coding agent."""
    if verbose:
        import logging
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        )


# ---------------------------------------------------------------------------
# 1. init
# ---------------------------------------------------------------------------

_DEFAULT_RULES = """\
# LocalForge Rules
# Add project-specific rules and conventions here.
# These will be included in the agent's context for every task.

# Example:
# - Always use type hints in Python code
# - Follow PEP 8 style guidelines
# - Write docstrings for all public functions
"""

_DEFAULT_COMMANDS = """\
# Custom verification commands
# LocalForge will auto-detect common tooling, but you can add extra commands here.

# commands:
#   - name: custom_lint
#     cmd: "your-linter ."
#     timeout: 60
#   - name: custom_test
#     cmd: "your-test-runner"
#     timeout: 300
"""


@app.command("init")
def init(
    repo_path: Path = typer.Argument(default=Path("."), help="Path to the repository root."),
) -> None:
    """Initialize a .localforge/ directory in the target repository."""
    import yaml

    from localforge.core.config import LocalForgeConfig

    repo = _resolve_repo(repo_path)
    target = repo / ".localforge"
    target.mkdir(parents=True, exist_ok=True)

    created: list[str] = []

    # config.yml
    config_path = target / "config.yml"
    if not config_path.exists():
        cfg = LocalForgeConfig()
        with open(config_path, "w", encoding="utf-8") as fh:
            yaml.dump(cfg.model_dump(mode="json"), fh, default_flow_style=False, sort_keys=False)
        created.append(str(config_path.relative_to(repo)))
    else:
        created.append(f"{config_path.relative_to(repo)} (already exists)")

    # rules.md
    rules_path = target / "rules.md"
    if not rules_path.exists():
        rules_path.write_text(_DEFAULT_RULES, encoding="utf-8")
        created.append(str(rules_path.relative_to(repo)))
    else:
        created.append(f"{rules_path.relative_to(repo)} (already exists)")

    # commands.yml
    commands_path = target / "commands.yml"
    if not commands_path.exists():
        commands_path.write_text(_DEFAULT_COMMANDS, encoding="utf-8")
        created.append(str(commands_path.relative_to(repo)))
    else:
        created.append(f"{commands_path.relative_to(repo)} (already exists)")

    body = "\n".join(f"  [green]•[/green] {f}" for f in created)
    console.print(
        Panel(
            f"[bold green]localforge initialized![/bold green]\n\n{body}",
            title="[bold]Init[/bold]",
            border_style="green",
            expand=False,
        )
    )


# ---------------------------------------------------------------------------
# 2. index
# ---------------------------------------------------------------------------


@app.command("index")
def index(
    force: bool = typer.Option(False, "--force", help="Re-index all files from scratch."),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
) -> None:
    """Index (or re-index) the repository for fast code retrieval."""
    from localforge.index import RepositoryIndexer

    repo = _resolve_repo(repo_path)
    config = _load_config(repo)

    db_path = repo / config.index_db_path
    indexer = RepositoryIndexer(repo, db_path, config)

    try:
        stats = indexer.index_repository(force=force)
    finally:
        indexer.close()

    table = Table(title="Indexing Results")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Total files found", str(stats["total_files"]))
    table.add_row("Files indexed", str(stats["indexed"]))
    table.add_row("Files skipped", str(stats["skipped"]))
    table.add_row("Errors", str(stats["errors"]))
    table.add_row("Duration", f"{stats['duration_seconds']}s")

    console.print(table)


# ---------------------------------------------------------------------------
# 3. analyze
# ---------------------------------------------------------------------------


@app.command("analyze")
def analyze(
    task: str = typer.Argument(
        None, help="Describe what you want to understand about the code.",
    ),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
    limit: int = typer.Option(10, "--limit", "-n", help="Max chunks to retrieve."),
    model: str | None = typer.Option(None, "--model", "-m", help="Override the Ollama model."),
) -> None:
    """Retrieve and display the code chunks most relevant to a task."""
    if not task:
        console.print(
            "[yellow]No task provided.[/yellow]\n"
            "Usage: [bold]localforge analyze \"describe what to analyze\"[/bold]\n"
            "Or use [bold cyan]localforge chat[/bold cyan] for interactive mode."
        )
        raise typer.Exit(1)
    from localforge.cli.display import print_chunks
    from localforge.context_manager.budget import TokenBudgetManager
    from localforge.retrieval import ContextRetriever

    repo = _resolve_repo(repo_path)
    overrides: dict[str, object] = {}
    if model is not None:
        overrides["model_name"] = model
    config = _load_config(repo, **overrides)

    _ensure_index(repo, config)

    indexer, searcher = _build_indexer_and_searcher(repo, config)

    try:
        retriever = ContextRetriever(indexer, searcher, config)
        result = retriever.retrieve(task, limit=limit)
    finally:
        searcher.close()
        indexer.close()

    console.print(
        f"\n[bold]Retrieved {len(result.chunks)} chunks[/bold] "
        f"(total found: {result.total_found})\n"
    )
    print_chunks(result.chunks)

    bm = TokenBudgetManager(config)
    total_tokens = sum(bm.count_tokens(c.content) for c in result.chunks)
    console.print(f"\n[dim]Total tokens across chunks: {total_tokens}[/dim]")


# ---------------------------------------------------------------------------
# 4. plan
# ---------------------------------------------------------------------------


@app.command("plan")
def plan(
    task: str = typer.Argument(
        None, help="Describe the coding task to plan.",
    ),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
    model: str | None = typer.Option(None, "--model", "-m", help="Override the Ollama model."),
) -> None:
    """Analyze a task and produce an execution plan (saved to .localforge/last_plan.json)."""
    if not task:
        console.print(
            "[yellow]No task provided.[/yellow]\n"
            "Usage: [bold]localforge plan \"describe the coding task\"[/bold]\n"
            "Or use [bold cyan]localforge chat[/bold cyan] for interactive mode."
        )
        raise typer.Exit(1)
    from localforge.agent.agents import AnalyzerAgent, PlannerAgent
    from localforge.agent.orchestrator import AgentOrchestrator
    from localforge.cli.display import print_plan
    from localforge.context_manager.assembler import ContextAssembler
    from localforge.context_manager.budget import TokenBudgetManager
    from localforge.core.models import AgentHandoff, AgentRole
    from localforge.core.ollama_client import OllamaClient
    from localforge.retrieval import ContextRetriever

    repo = _resolve_repo(repo_path)
    overrides: dict[str, object] = {}
    if model is not None:
        overrides["model_name"] = model
    config = _load_config(repo, **overrides)

    if not _check_ollama(config):
        raise typer.Exit(1)

    _ensure_index(repo, config)

    indexer, searcher = _build_indexer_and_searcher(repo, config)
    ollama = OllamaClient(config)
    bm = TokenBudgetManager(config)
    asm = ContextAssembler(bm, config)
    retriever = ContextRetriever(indexer, searcher, config)

    try:
        async def _run_plan() -> dict[str, Any]:
            # Phase 1: Retrieve context
            retrieval_result = retriever.retrieve(task, limit=15)
            chunks = retrieval_result.chunks

            # Phase 2: Analyze
            console.print("[bold cyan]>> Analyzing task...[/bold cyan]")
            analyzer = AnalyzerAgent(ollama, asm, bm, config)
            analyzer_handoff = AgentHandoff(
                from_role=AgentRole.ORCHESTRATOR,
                to_role=AgentRole.ANALYZER,
                payload={"task": task, "repo_path": str(repo)},
                context_chunks=chunks,
                instruction="Analyze this task",
            )
            analysis_msg = await analyzer.execute(analyzer_handoff)
            analysis = analysis_msg.structured_data or {}

            # Phase 3: Plan
            console.print("[bold cyan]>> Creating execution plan...[/bold cyan]")
            planner = PlannerAgent(ollama, asm, bm, config)
            planner_handoff = AgentHandoff(
                from_role=AgentRole.ORCHESTRATOR,
                to_role=AgentRole.PLANNER,
                payload={"task": task, "analysis": analysis},
                context_chunks=chunks,
                instruction="Create execution plan",
            )
            plan_msg = await planner.execute(planner_handoff)
            return plan_msg.structured_data or {}

        plan_data = asyncio.run(_run_plan())

        # Build plan model
        agent_plan = AgentOrchestrator._build_plan(plan_data)
        print_plan(agent_plan)

        # Save plan to disk
        plan_path = repo / ".localforge" / "last_plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            json.dumps(plan_data, indent=2, default=str),
            encoding="utf-8",
        )
        console.print(f"\n[green]Plan saved to {plan_path.relative_to(repo)}[/green]")

    finally:
        searcher.close()
        indexer.close()


# ---------------------------------------------------------------------------
# 5. patch
# ---------------------------------------------------------------------------


@app.command("patch")
def patch(
    task: str = typer.Argument(
        None, help="Describe the coding task.",
    ),
    step: int | None = typer.Option(None, "--step", "-s", help="Execute only this step number."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show patches without applying."),
    auto_approve: bool = typer.Option(False, "--yes", "-y", help="Auto-approve all patches."),
    model: str | None = typer.Option(None, "--model", "-m", help="Override the Ollama model."),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
) -> None:
    """Execute plan steps: generate and apply patches."""
    if not task:
        console.print(
            "[yellow]No task provided.[/yellow]\n"
            "Usage: [bold]localforge patch \"describe the coding task\"[/bold]\n"
            "Or use [bold cyan]localforge chat[/bold cyan] for interactive mode."
        )
        raise typer.Exit(1)
    from localforge.agent.agents import CoderAgent
    from localforge.agent.orchestrator import AgentOrchestrator
    from localforge.cli.display import confirm_patch as confirm_patch_fn
    from localforge.context_manager.assembler import ContextAssembler
    from localforge.context_manager.budget import TokenBudgetManager
    from localforge.core.models import AgentHandoff, AgentRole
    from localforge.core.ollama_client import OllamaClient
    from localforge.patching.patcher import FilePatcher

    repo = _resolve_repo(repo_path)
    overrides: dict[str, object] = {
        "dry_run": dry_run,
        "auto_approve": auto_approve,
    }
    if model is not None:
        overrides["model_name"] = model
    config = _load_config(repo, **overrides)

    # Load last plan
    plan_path = repo / ".localforge" / "last_plan.json"
    if not plan_path.is_file():
        console.print(
            "[bold red]No plan found.[/bold red] Run [bold]localforge plan[/bold] first."
        )
        raise typer.Exit(1)

    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    agent_plan = AgentOrchestrator._build_plan(plan_data)

    if not agent_plan.steps:
        console.print("[yellow]Plan has no steps.[/yellow]")
        raise typer.Exit(0)

    # Filter to a single step if requested
    steps_to_run = agent_plan.steps
    if step is not None:
        steps_to_run = [s for s in agent_plan.steps if s.step_id == step]
        if not steps_to_run:
            console.print(f"[bold red]Step {step} not found in plan.[/bold red]")
            raise typer.Exit(1)

    if not _check_ollama(config):
        raise typer.Exit(1)

    ollama = OllamaClient(config)
    bm = TokenBudgetManager(config)
    asm = ContextAssembler(bm, config)
    patcher = FilePatcher(repo, config)

    async def _run_patch() -> None:
        try:
            coder = CoderAgent(ollama, asm, bm, config)

            for plan_step in steps_to_run:
                console.print(
                    f"\n[bold]-> Step {plan_step.step_id}:[/bold] {plan_step.description}"
                )

                # Read the primary file content
                file_content = ""
                if plan_step.files_involved:
                    primary = repo / plan_step.files_involved[0]
                    if primary.is_file():
                        file_content = primary.read_text(encoding="utf-8")

                coder_handoff = AgentHandoff(
                    from_role=AgentRole.ORCHESTRATOR,
                    to_role=AgentRole.CODER,
                    payload={
                        "task": task,
                        "step": plan_step.model_dump(),
                        "file_path": (
                            plan_step.files_involved[0]
                            if plan_step.files_involved
                            else ""
                        ),
                        "file_content": file_content,
                    },
                    context_chunks=[],
                    instruction="Implement this step",
                )
                coder_msg = await coder.execute(coder_handoff)

                if not coder_msg.success:
                    console.print(f"[red]Coder failed: {coder_msg.error or 'unknown'}[/red]")
                    continue

                patch_op = AgentOrchestrator._parse_patch_operation(
                    coder_msg.structured_data or {}
                )

                # Show diff
                patcher.show_diff(patch_op)

                if dry_run:
                    console.print("[yellow]Dry-run — patch not applied.[/yellow]")
                    continue

                # Confirm
                if not auto_approve and not confirm_patch_fn(patch_op):
                    console.print("[dim]Skipped.[/dim]")
                    continue

                if patcher.apply_patch(patch_op):
                    console.print(
                        f"[bold green][OK] Patch applied to"
                        f" {patch_op.file_path}[/bold green]"
                    )
                else:
                    console.print(
                        f"[bold red][FAIL] Failed to apply patch to"
                        f" {patch_op.file_path}[/bold red]"
                    )
        finally:
            await ollama.close()

    asyncio.run(_run_patch())


# ---------------------------------------------------------------------------
# 6. verify
# ---------------------------------------------------------------------------


@app.command("verify")
def verify(
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
) -> None:
    """Run verification checks (lint, type-check, tests) on the repository."""
    from localforge.cli.display import print_verification_results
    from localforge.verifier.runner import VerificationRunner

    repo = _resolve_repo(repo_path)
    config = _load_config(repo)

    runner = VerificationRunner(repo, config)
    results = runner.run_verification()
    print_verification_results(results)

    summary = runner.summarize_results(results)
    if summary["all_passed"]:
        console.print("\n[bold green]All checks passed![/bold green]")
    else:
        console.print(
            f"\n[bold red]{summary['total_errors']} error(s), "
            f"{summary['total_warnings']} warning(s)[/bold red]"
        )
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# 7. autofix — THE MAIN COMMAND
# ---------------------------------------------------------------------------


@app.command("autofix")
def autofix(
    task: str = typer.Argument(
        None, help="Describe the coding task to perform.",
    ),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
    auto_approve: bool = typer.Option(False, "--yes", "-y", help="Auto-approve all patches."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show patches without applying them."),
    max_iterations: int | None = typer.Option(
        None, "--max-iterations", help="Override max agent iterations."
    ),
    model: str | None = typer.Option(None, "--model", "-m", help="Override the Ollama model."),
    profile: str | None = typer.Option(
        None, "--profile", "-p", help="Model profile: small, medium, large."
    ),
) -> None:
    """Run the full agent pipeline: analyze, plan, patch, verify, and iterate."""
    if not task:
        console.print(
            "[yellow]No task provided.[/yellow]\n"
            "Usage: [bold]localforge autofix \"describe the task\"[/bold]\n"
            "Or use [bold cyan]localforge chat[/bold cyan] for interactive mode."
        )
        raise typer.Exit(1)
    from localforge.agent.orchestrator import AgentOrchestrator
    from localforge.cli.display import print_banner
    from localforge.context_manager.assembler import ContextAssembler
    from localforge.context_manager.budget import TokenBudgetManager
    from localforge.core.config import ModelProfile
    from localforge.core.ollama_client import OllamaClient
    from localforge.patching.patcher import FilePatcher
    from localforge.retrieval import ContextRetriever
    from localforge.verifier.runner import VerificationRunner

    repo = _resolve_repo(repo_path)

    overrides: dict[str, object] = {
        "auto_approve": auto_approve,
        "dry_run": dry_run,
    }
    if model is not None:
        overrides["model_name"] = model
    if profile is not None:
        overrides["model_profile"] = ModelProfile(profile)
    if max_iterations is not None:
        overrides["max_iterations"] = max_iterations

    config = _load_config(repo, **overrides)

    print_banner(version=__version__, model=config.model_name)

    console.print(f"  [bold]Profile :[/bold] {config.model_profile.value}")
    console.print(f"  [bold]Repo    :[/bold] {repo}")
    console.print(f"  [bold]Task    :[/bold] {task}")
    if dry_run:
        console.print("  [yellow]Dry-run mode — no files will be modified.[/yellow]")
    console.print()

    # Check Ollama connectivity
    if not _check_ollama(config):
        raise typer.Exit(1)

    # Auto-detect model context window
    from localforge.core.ollama_client import OllamaClient as _OllamaClient

    async def _detect_ctx() -> int:
        _client = _OllamaClient(config)
        try:
            return await _client.detect_context_window()
        finally:
            await _client.close()

    detected_ctx = asyncio.run(_detect_ctx())
    if detected_ctx != config.max_context_tokens:
        config = config.model_copy(update={"max_context_tokens": detected_ctx})
        console.print(f"  [bold]Context :[/bold] {detected_ctx} tokens (auto-detected)")

    # Ensure the repository is indexed
    _ensure_index(repo, config)

    # Git checkpoint before changes
    from localforge.core.git_utils import create_checkpoint, is_git_repo

    is_git = is_git_repo(repo)
    if is_git and not dry_run:
        sha = create_checkpoint(repo, f"localforge: pre-autofix checkpoint ({task[:50]})")
        if sha:
            console.print(f"  [bold]Git    :[/bold] checkpoint created ({sha[:8]})")

    # Build the full agent stack
    ollama = OllamaClient(config)
    bm = TokenBudgetManager(config)
    asm = ContextAssembler(bm, config)

    indexer, searcher = _build_indexer_and_searcher(repo, config)
    retriever = ContextRetriever(indexer, searcher, config)
    patcher = FilePatcher(repo, config)
    verifier_runner = VerificationRunner(repo, config)

    orchestrator = AgentOrchestrator(
        config=config,
        ollama=ollama,
        retriever=retriever,
        assembler=asm,
        budget_manager=bm,
        patcher=patcher,
        verifier_runner=verifier_runner,
    )

    try:
        async def _run_autofix() -> MultiAgentState:
            result = await orchestrator.run(task)
            await ollama.close()
            return result

        final_state = asyncio.run(_run_autofix())
    finally:
        searcher.close()
        indexer.close()

    # Show final state
    if final_state:
        from localforge.agent.display import OrchestratorDisplay

        OrchestratorDisplay().show_summary(final_state)

    # Git checkpoint after changes
    if is_git and not dry_run:
        sha = create_checkpoint(repo, f"localforge: {task[:70]}")
        if sha:
            console.print(f"\n[bold green]Git commit created:[/bold green] {sha[:8]}")


# ---------------------------------------------------------------------------
# 8. diff
# ---------------------------------------------------------------------------


@app.command("diff")
def diff(
    backup_timestamp: str | None = typer.Argument(
        None, help="Show diffs for a specific backup timestamp. Omit to list available backups."
    ),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
) -> None:
    """Show diffs for changes made by localforge."""
    import difflib as _difflib

    from localforge.cli.display import print_diff

    repo = _resolve_repo(repo_path)
    backup_root = repo / ".localforge" / "backups"

    if not backup_root.is_dir():
        console.print("[dim]No backups found. Nothing to diff.[/dim]")
        raise typer.Exit(0)

    available = sorted(
        [d.name for d in backup_root.iterdir() if d.is_dir()],
        reverse=True,
    )

    if not available:
        console.print("[dim]No backup timestamps found.[/dim]")
        raise typer.Exit(0)

    if backup_timestamp is None:
        # Show most recent backup
        backup_timestamp = available[0]
        console.print(f"[bold]Showing diffs for latest backup:[/bold] {backup_timestamp}\n")
        if len(available) > 1:
            console.print(f"[dim]Other available backups: {', '.join(available[1:5])}[/dim]\n")

    backup_dir = backup_root / backup_timestamp
    if not backup_dir.is_dir():
        console.print(f"[bold red]Backup not found:[/bold red] {backup_timestamp}")
        console.print(f"[dim]Available: {', '.join(available[:10])}[/dim]")
        raise typer.Exit(1)

    # Walk backup files and diff each against current
    found_any = False
    for backup_file in sorted(backup_dir.rglob("*")):
        if not backup_file.is_file():
            continue
        rel = backup_file.relative_to(backup_dir)
        current_file = repo / rel

        old_content = backup_file.read_text(encoding="utf-8", errors="replace")
        new_content = (
            current_file.read_text(encoding="utf-8", errors="replace")
            if current_file.is_file()
            else ""
        )

        diff_lines = list(
            _difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"backup/{rel}",
                tofile=str(rel),
            )
        )
        if diff_lines:
            found_any = True
            console.print(f"\n[bold]{rel}[/bold]")
            print_diff("".join(diff_lines))

    if not found_any:
        console.print("[dim]No differences found between backup and current files.[/dim]")


# ---------------------------------------------------------------------------
# 9. status
# ---------------------------------------------------------------------------


@app.command("status")
def status(
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
) -> None:
    """Show project status: index stats, Ollama health, model info, and last task."""
    from localforge.core.ollama_client import OllamaClient
    from localforge.index import RepositoryIndexer

    repo = _resolve_repo(repo_path)
    config = _load_config(repo)
    localforge_dir = repo / ".localforge"

    # -- Initialization status -----------------------------------------
    initialized = localforge_dir.is_dir()
    console.print(
        Panel(
            f"[bold]Initialized:[/bold] "
            f"{'[green]Yes[/green]' if initialized else '[red]No[/red]'}\n"
            f"[bold]Repo path  :[/bold] {repo}\n"
            f"[bold]Model      :[/bold] {config.model_name}\n"
            f"[bold]Profile    :[/bold] {config.model_profile.value}",
            title="[bold]LocalForge Status[/bold]",
            border_style="cyan",
            expand=False,
        )
    )

    # -- Index stats ---------------------------------------------------
    db_path = repo / config.index_db_path
    if db_path.is_file():
        indexer = RepositoryIndexer(repo, db_path, config)
        try:
            idx_stats = indexer.get_stats()
            table = Table(title="Index Statistics")
            table.add_column("Metric", style="bold")
            table.add_column("Value", justify="right")
            table.add_row("Files", str(idx_stats["total_files"]))
            table.add_row("Chunks", str(idx_stats["total_chunks"]))
            table.add_row("Symbols", str(idx_stats["total_symbols"]))
            for lang, count in idx_stats.get("languages", {}).items():
                table.add_row(f"  {lang}", str(count))
            console.print(table)
        finally:
            indexer.close()
    else:
        console.print("[yellow]Index not built yet. Run:[/yellow] localforge index")

    # -- Ollama status -------------------------------------------------
    async def _check_ollama_status() -> tuple[bool, list[str]]:
        ollama = OllamaClient(config)
        try:
            healthy = await ollama.health_check()
            models = await ollama.list_models() if healthy else []
            return healthy, models
        finally:
            await ollama.close()

    healthy, models = asyncio.run(_check_ollama_status())
    if healthy:
        model_str = ", ".join(models[:10]) if models else "(none)"
        console.print(
            f"\n[bold green]Ollama:[/bold green] connected at {config.ollama_base_url}"
        )
        console.print(f"[bold]Available models:[/bold] {model_str}")
    else:
        console.print(
            f"\n[bold red]Ollama:[/bold red] not reachable at {config.ollama_base_url}"
        )

    # -- Last task info ------------------------------------------------
    plan_path = repo / ".localforge" / "last_plan.json"
    if plan_path.is_file():
        try:
            plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
            num_steps = len(plan_data.get("steps", []))
            console.print(
                f"\n[bold]Last plan:[/bold] {num_steps} step(s) "
                f"— complexity: {plan_data.get('estimated_complexity', 'unknown')}"
            )
        except (json.JSONDecodeError, OSError):
            pass

    # -- State files ---------------------------------------------------
    states_dir = repo / ".localforge" / "states"
    if states_dir.is_dir():
        state_files = list(states_dir.glob("*.json"))
        if state_files:
            console.print(f"[dim]Saved state snapshots: {len(state_files)}[/dim]")

    # -- Git info ------------------------------------------------------
    from localforge.core.git_utils import get_changed_files, get_current_branch, is_git_repo

    if is_git_repo(repo):
        branch = get_current_branch(repo)
        changed = get_changed_files(repo)
        console.print(f"\n[bold]Git branch:[/bold] {branch or '(detached)'}")
        if changed:
            console.print(f"[bold]Changed files:[/bold] {len(changed)}")
            for f in changed[:10]:
                console.print(f"  [yellow]{f}[/yellow]")
            if len(changed) > 10:
                console.print(f"  [dim]… and {len(changed) - 10} more[/dim]")
        else:
            console.print("[dim]Working tree clean.[/dim]")


# ---------------------------------------------------------------------------
# 10. chat — interactive REPL
# ---------------------------------------------------------------------------


@app.command("chat")
def chat(
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
    model: str | None = typer.Option(None, "--model", "-m", help="Override the Ollama model."),
) -> None:
    """Start an interactive chat session about your codebase."""
    from localforge.chat.engine import ChatEngine
    from localforge.core.ollama_client import OllamaClient

    repo = _resolve_repo(repo_path)
    overrides: dict[str, object] = {}
    if model is not None:
        overrides["model_name"] = model
    config = _load_config(repo, **overrides)

    if not _check_ollama(config):
        raise typer.Exit(1)

    # Auto-detect model context window
    async def _detect_ctx() -> int:
        _client = OllamaClient(config)
        try:
            return await _client.detect_context_window()
        finally:
            await _client.close()

    detected_ctx = asyncio.run(_detect_ctx())
    if detected_ctx > config.max_context_tokens:
        # Use the model's actual context window if larger than the default
        config = config.model_copy(update={"max_context_tokens": detected_ctx})
        console.print(
            f"[dim]Context window: {detected_ctx:,} tokens (auto-detected from model)[/dim]"
        )

    _ensure_index(repo, config)

    ollama = OllamaClient(config)
    engine = ChatEngine(config, ollama, repo)

    async def _run_chat() -> None:
        try:
            await engine.run_repl()
        finally:
            await ollama.close()

    asyncio.run(_run_chat())


# ---------------------------------------------------------------------------
# 11. rollback
# ---------------------------------------------------------------------------


@app.command("rollback")
def rollback(
    backup_timestamp: str | None = typer.Argument(
        None, help="Backup timestamp to rollback to. Omit to list available backups."
    ),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
) -> None:
    """Rollback file changes to a previous backup state."""
    from localforge.patching.patcher import FilePatcher

    repo = _resolve_repo(repo_path)
    config = _load_config(repo)
    backup_root = repo / ".localforge" / "backups"

    if not backup_root.is_dir():
        console.print("[dim]No backups found. Nothing to rollback.[/dim]")
        raise typer.Exit(0)

    available = sorted(
        [d.name for d in backup_root.iterdir() if d.is_dir()],
        reverse=True,
    )

    if not available:
        console.print("[dim]No backup timestamps found.[/dim]")
        raise typer.Exit(0)

    if backup_timestamp is None:
        console.print("[bold]Available backups:[/bold]")
        for ts in available[:20]:
            console.print(f"  {ts}")
        console.print("\n[dim]Use: localforge rollback <timestamp>[/dim]")
        raise typer.Exit(0)

    patcher = FilePatcher(repo, config)
    if patcher.rollback(backup_timestamp):
        console.print(f"[bold green]Successfully rolled back to {backup_timestamp}[/bold green]")
    else:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# 11. search
# ---------------------------------------------------------------------------


@app.command("search")
def search(
    query: str = typer.Argument(..., help="Search query (text, filename, or symbol)."),
    mode: str = typer.Option(
        "all", "--mode", "-m",
        help="Search mode: all, text, filename, symbol.",
    ),
    limit: int = typer.Option(10, "--limit", "-n", help="Max results."),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
) -> None:
    """Search the codebase index for text, filenames, or symbols."""
    from localforge.cli.display import print_chunks
    from localforge.index import IndexSearcher

    repo = _resolve_repo(repo_path)
    config = _load_config(repo)

    db_path = repo / config.index_db_path
    if not db_path.is_file():
        console.print(
            "[bold red]No index found.[/bold red] "
            "Run [bold]localforge index[/bold] first."
        )
        raise typer.Exit(1)

    searcher = IndexSearcher(db_path)

    try:
        if mode in ("all", "text"):
            results = searcher.search_lexical(query, limit=limit)
            if results:
                console.print(f"\n[bold cyan]Text matches ({len(results)}):[/bold cyan]")
                print_chunks(results)

        if mode in ("all", "filename"):
            results = searcher.search_by_filename(query, limit=limit)
            if results:
                console.print(f"\n[bold cyan]Filename matches ({len(results)}):[/bold cyan]")
                for r in results:
                    console.print(f"  [green]{r.file_path}[/green] (score: {r.score:.2f})")

        if mode in ("all", "symbol"):
            sym_results = searcher.search_symbols(query)
            if sym_results:
                console.print(f"\n[bold cyan]Symbol matches ({len(sym_results)}):[/bold cyan]")
                for s in sym_results[:limit]:
                    console.print(
                        f"  [yellow]{s['kind']}[/yellow] [bold]{s['name']}[/bold] "
                        f"in [green]{s['file_path']}[/green] line {s['line']}"
                    )

        if mode not in ("all", "text", "filename", "symbol"):
            console.print(f"[red]Unknown mode: {mode}. Use: all, text, filename, symbol[/red]")
            raise typer.Exit(1)

    finally:
        searcher.close()


# ---------------------------------------------------------------------------
# 12. models — list available models
# ---------------------------------------------------------------------------


@app.command("models")
def models(
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
) -> None:
    """List all available models on your Ollama instance."""
    from localforge.core.ollama_client import OllamaClient

    repo = _resolve_repo(repo_path)
    config = _load_config(repo)

    async def _list_models() -> list[str]:
        client = OllamaClient(config)
        try:
            return await client.list_models()
        finally:
            await client.close()

    if not _check_ollama(config):
        raise typer.Exit(1)

    model_list = asyncio.run(_list_models())

    if not model_list:
        console.print("[yellow]No models found on your Ollama instance.[/yellow]")
        console.print("[dim]Run: ollama pull <model-name>[/dim]")
        raise typer.Exit(0)

    current_model = config.model_name
    table = Table(title="Available Models on Ollama", show_header=True)
    table.add_column("Model", style="cyan")
    table.add_column("Status", justify="center")

    for model_name in sorted(model_list):
        is_current = "✓ current" if model_name == current_model else ""
        style = "green" if is_current else ""
        table.add_row(model_name, is_current, style=style)

    console.print(table)
    console.print(
        f"\n[bold]Current default model:[/bold] [cyan]{current_model}[/cyan]"
    )
    console.print("[dim]Tip: Use 'localforge set-model' to change the default[/dim]")


# ---------------------------------------------------------------------------
# 13. set-model — set default model
# ---------------------------------------------------------------------------


@app.command("set-model")
def set_model(
    model_name: str | None = typer.Argument(
        None,
        help="Model name to set as default (e.g., qwen2.5-coder:14b). "
        "If omitted, shows available models for interactive selection.",
    ),
    repo_path: Path = typer.Option(
        Path("."), "--repo", "-r", help="Path to the repository root.",
    ),
) -> None:
    """Set your default model or interactively choose from available models."""
    from localforge.core.ollama_client import OllamaClient

    repo = _resolve_repo(repo_path)
    config = _load_config(repo)
    localforge_dir = repo / ".localforge"

    if not localforge_dir.is_dir():
        console.print(
            "[bold red]Error:[/bold red] .localforge directory not found.\n"
            "[dim]Run 'localforge init' first.[/dim]"
        )
        raise typer.Exit(1)

    async def _fetch_models() -> list[str]:
        client = OllamaClient(config)
        try:
            return await client.list_models()
        finally:
            await client.close()

    if not _check_ollama(config):
        raise typer.Exit(1)

    available_models = asyncio.run(_fetch_models())

    if not available_models:
        console.print("[yellow]No models found on your Ollama instance.[/yellow]")
        console.print("[dim]Run: ollama pull <model-name>[/dim]")
        raise typer.Exit(0)

    # If model name is provided, use it directly
    selected_model = model_name
    if not selected_model:
        # Interactive selection
        console.print("\n[bold cyan]Available models:[/bold cyan]")
        for i, m in enumerate(sorted(available_models), 1):
            current_marker = " ← current" if m == config.model_name else ""
            console.print(f"  {i:2d}. {m}{current_marker}")

        console.print()
        try:
            choice = typer.prompt("Select model number", type=int)
            if 1 <= choice <= len(available_models):
                sorted_models = sorted(available_models)
                selected_model = sorted_models[choice - 1]
            else:
                console.print("[red]Invalid choice.[/red]")
                raise typer.Exit(1)
        except ValueError:
            console.print("[red]Invalid input. Please enter a number.[/red]")
            raise typer.Exit(1) from None
    else:
        # Validate that the provided model exists
        if selected_model not in available_models:
            console.print(
                f"[red]Error:[/red] Model '{selected_model}' not found on your Ollama instance.\n"
                f"[dim]Available models: {', '.join(sorted(available_models)[:5])}...[/dim]"
            )
            raise typer.Exit(1)

    # Update config file while preserving existing comments/formatting.
    config_path = localforge_dir / "config.yml"
    try:
        existing_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing_text = ""

    model_line = f'model_name: "{selected_model}"'
    if existing_text:
        pattern = re.compile(r"^\s*model_name\s*:\s*.*$", re.MULTILINE)
        if pattern.search(existing_text):
            new_text = pattern.sub(model_line, existing_text, count=1)
        else:
            new_text = f"{model_line}\n{existing_text}"
    else:
        new_text = f"{model_line}\n"

    config_path.write_text(new_text, encoding="utf-8")

    console.print(
        f"\n[green]✓ Default model updated to:[/green] "
        f"[bold cyan]{selected_model}[/bold cyan]"
    )
    console.print(f"[dim]Saved to: {config_path.relative_to(repo)}[/dim]")


# ---------------------------------------------------------------------------
# 14. history — show past task runs
# ---------------------------------------------------------------------------


@app.command("history")
def history(
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
) -> None:
    """Show history of previous localforge task runs."""
    from localforge.agent.state_manager import StateManager

    repo = _resolve_repo(repo_path)
    mgr = StateManager(str(repo / ".localforge" / "states"))
    states = mgr.list_states()

    if not states:
        console.print("[dim]No task history found.[/dim]")
        raise typer.Exit(0)

    table = Table(title="Task History")
    table.add_column("#", style="bold", width=4)
    table.add_column("Task")
    table.add_column("Iterations", justify="right", width=10)
    table.add_column("Messages", justify="right", width=10)

    for i, s in enumerate(states[:20], 1):
        task_preview = s["task"][:80] + "…" if len(s["task"]) > 80 else s["task"]
        table.add_row(
            str(i),
            task_preview,
            str(s["iteration"]),
            str(s["messages"]),
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Legacy command aliases
# ---------------------------------------------------------------------------


@app.command("config-show", hidden=True)
def config_show(
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
) -> None:
    """Display the resolved configuration."""
    from rich.pretty import pprint

    from localforge.core.config import load_config

    cfg = load_config(str(repo_path))
    pprint(cfg.model_dump(), expand_all=True)


# Keep the old 'run' as an alias for 'autofix'
@app.command("run", hidden=True)
def run(
    task: str = typer.Argument(..., help="Describe the coding task to perform."),
    model: str | None = typer.Option(None, "--model", "-m", help="Override the Ollama model."),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
    auto_approve: bool = typer.Option(False, "--yes", "-y", help="Auto-approve all patches."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show patches without applying them."),
    profile: str | None = typer.Option(
        None, "--profile", "-p", help="Model profile: small, medium, large."
    ),
) -> None:
    """Run the agent on a task (alias for autofix)."""
    autofix(
        task=task,
        repo_path=repo_path,
        auto_approve=auto_approve,
        dry_run=dry_run,
        model=model,
        profile=profile,
    )


# ---------------------------------------------------------------------------
# 15. cloud-chat — cloud-powered autonomous chat
# ---------------------------------------------------------------------------


@app.command("cloud-chat")
def cloud_chat(
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
    fresh_auth: bool = typer.Option(
        False, "--fresh-auth", help="Force re-entering authentication headers."
    ),
) -> None:
    """Start an interactive cloud-powered chat session (Gemini 3.1 Pro).

    Uses a cloud API for fast, powerful, autonomous coding.
    Auth headers are pasted from your browser at runtime — nothing is hardcoded.
    """
    from localforge.cli.display import print_banner
    from localforge.cloud.auth import CredentialStore, validate_headers
    from localforge.cloud.client import CloudClient
    from localforge.cloud.engine import CloudChatEngine

    repo = _resolve_repo(repo_path)
    config = _load_config(repo)

    # Upgrade context config for cloud model
    config = config.model_copy(update={"max_context_tokens": 131072})

    # --- Auth ---
    cred_store = CredentialStore(repo_path=repo)

    auth_data = None
    if not fresh_auth:
        auth_data = cred_store.load()
        if auth_data and cred_store.is_expired(auth_data):
            console.print(
                "[yellow]Cached credentials have expired. Please paste fresh headers.[/yellow]"
            )
            auth_data = None

    if auth_data is None:
        try:
            auth_data = cred_store.prompt_for_headers()
        except (ValueError, KeyboardInterrupt) as exc:
            console.print(f"[bold red]Authentication failed:[/bold red] {exc}")
            raise typer.Exit(1) from None

    ok, msg = validate_headers(auth_data)
    if not ok:
        console.print(f"[bold red]Invalid headers:[/bold red] {msg}")
        raise typer.Exit(1)

    # --- Build client & engine ---
    client = CloudClient(auth_data)

    # --- Index ---
    _ensure_index(repo, config)

    # --- Run ---
    engine = CloudChatEngine(config, client, repo, credential_store=cred_store)

    print_banner(version=__version__, model="gemini-3.1-pro-preview (cloud)")

    async def _run() -> None:
        # Health check and REPL must share the same event loop so httpx
        # connections created during the health check remain usable.
        from localforge.cloud.exceptions import AuthExpiredError, VPNError

        try:
            healthy = False
            for _hc_attempt in range(2):
                try:
                    healthy = await client.health_check()
                    break
                except AuthExpiredError:
                    # Cached credentials are stale — prompt for fresh ones
                    console.print(
                        "[yellow]Cached credentials expired. Please paste fresh headers.[/yellow]"
                    )
                    try:
                        fresh = cred_store.prompt_for_headers()
                        client._headers.update(fresh.get("headers", {}))
                        with contextlib.suppress(Exception):
                            await client._client.aclose()
                        client._client = client._new_httpx_client()
                        client.reset_conversation()
                        continue  # retry health check
                    except (ValueError, KeyboardInterrupt):
                        console.print("[red]Authentication cancelled.[/red]")
                        return
                except VPNError:
                    # Network / DNS issue — warn but let REPL start;
                    # individual requests have their own retry logic.
                    console.print(
                        "[yellow]⚠ DNS/network is unstable — starting anyway (requests will auto-retry).[/yellow]"
                    )
                    healthy = True  # allow REPL to proceed
                    break
                except Exception as exc:
                    console.print(f"[bold red]Health check failed:[/bold red] {exc}")
                    return

            if not healthy:
                console.print("[bold red]Health check returned unhealthy.[/bold red]")
                return

            await engine.run_repl()
        finally:
            await client.close()

    asyncio.run(_run())
