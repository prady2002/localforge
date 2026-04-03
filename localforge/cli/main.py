"""CLI entry-point for localforge."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows to prevent UnicodeEncodeError from Rich
# rendering Unicode spinners and box-drawing characters.
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

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


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]localforge[/bold] {__version__}")
        raise typer.Exit()


def _resolve_repo(repo_path: Path) -> Path:
    return Path(repo_path).resolve()


def _load_config(repo_path: Path, **overrides: object):  # noqa: ANN202
    from localforge.core.config import LocalForgeConfig, load_config

    cfg = load_config(str(repo_path))
    # Ensure repo_path is set from the function argument (callers may also
    # pass it via **overrides, which would cause a duplicate-keyword error).
    overrides["repo_path"] = str(repo_path)
    cfg = LocalForgeConfig(**{**cfg.model_dump(), **overrides})
    return cfg


def _check_ollama(config) -> bool:  # noqa: ANN001
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


def _build_indexer_and_searcher(repo_path: Path, config):  # noqa: ANN001
    from localforge.index import IndexSearcher, RepositoryIndexer

    db_path = repo_path / config.index_db_path
    indexer = RepositoryIndexer(repo_path, db_path, config)
    searcher = IndexSearcher(db_path)
    return indexer, searcher


def _ensure_index(repo_path: Path, config) -> None:  # noqa: ANN001
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
) -> None:
    """localforge — local-first, repo-aware coding agent."""


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

    if not _check_ollama(config):
        raise typer.Exit(1)

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
    task: str = typer.Argument(..., help="Describe what you want to understand about the code."),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
    limit: int = typer.Option(10, "--limit", "-n", help="Max chunks to retrieve."),
) -> None:
    """Retrieve and display the code chunks most relevant to a task."""
    from localforge.cli.display import print_chunks
    from localforge.context_manager.budget import TokenBudgetManager
    from localforge.retrieval import ContextRetriever

    repo = _resolve_repo(repo_path)
    config = _load_config(repo)

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
    task: str = typer.Argument(..., help="Describe the coding task to plan."),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
) -> None:
    """Analyze a task and produce an execution plan (saved to .localforge/last_plan.json)."""
    from localforge.agent.agents import AnalyzerAgent, PlannerAgent
    from localforge.agent.orchestrator import AgentOrchestrator
    from localforge.cli.display import print_plan
    from localforge.context_manager.assembler import ContextAssembler
    from localforge.context_manager.budget import TokenBudgetManager
    from localforge.core.models import AgentHandoff, AgentRole
    from localforge.core.ollama_client import OllamaClient
    from localforge.retrieval import ContextRetriever

    repo = _resolve_repo(repo_path)
    config = _load_config(repo)

    if not _check_ollama(config):
        raise typer.Exit(1)

    _ensure_index(repo, config)

    indexer, searcher = _build_indexer_and_searcher(repo, config)
    ollama = OllamaClient(config)
    bm = TokenBudgetManager(config)
    asm = ContextAssembler(bm, config)
    retriever = ContextRetriever(indexer, searcher, config)

    try:
        async def _run_plan():
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
    task: str = typer.Argument(..., help="Describe the coding task."),
    step: int | None = typer.Option(None, "--step", "-s", help="Execute only this step number."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show patches without applying."),
    auto_approve: bool = typer.Option(False, "--yes", "-y", help="Auto-approve all patches."),
    repo_path: Path = typer.Option(Path("."), "--repo", "-r", help="Path to the repository root."),
) -> None:
    """Execute plan steps: generate and apply patches."""
    from localforge.agent.agents import CoderAgent
    from localforge.agent.orchestrator import AgentOrchestrator
    from localforge.cli.display import confirm_patch as confirm_patch_fn
    from localforge.context_manager.assembler import ContextAssembler
    from localforge.context_manager.budget import TokenBudgetManager
    from localforge.core.models import AgentHandoff, AgentRole
    from localforge.core.ollama_client import OllamaClient
    from localforge.patching.patcher import FilePatcher

    repo = _resolve_repo(repo_path)
    config = _load_config(repo, dry_run=dry_run, auto_approve=auto_approve)

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

    try:
        async def _run_patch():
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
                        "file_path": plan_step.files_involved[0] if plan_step.files_involved else "",
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
                if not auto_approve:
                    if not confirm_patch_fn(patch_op):
                        console.print("[dim]Skipped.[/dim]")
                        continue

                if patcher.apply_patch(patch_op):
                    console.print(f"[bold green][OK] Patch applied to {patch_op.file_path}[/bold green]")
                else:
                    console.print(f"[bold red][FAIL] Failed to apply patch to {patch_op.file_path}[/bold red]")

            await ollama.close()

        asyncio.run(_run_patch())

    finally:
        pass


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
    task: str = typer.Argument(..., help="Describe the coding task to perform."),
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

    # Ensure the repository is indexed
    _ensure_index(repo, config)

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
        async def _run_autofix():
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
        if current_file.is_file():
            new_content = current_file.read_text(encoding="utf-8", errors="replace")
        else:
            new_content = ""

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
            f"[bold]Initialized:[/bold] {'[green]Yes[/green]' if initialized else '[red]No[/red]'}\n"
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
    async def _check_ollama_status():
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
