"""Rich display utilities for the localforge CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

if TYPE_CHECKING:
    from localforge.core.models import (
        AgentPlan,
        AgentState,
        FileChunk,
        PatchOperation,
        VerificationResult,
    )

console = Console()

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

_BANNER = r"""
 _                    _  __
| |    ___   ___ __ _| |/ _| ___  _ __ __ _  ___
| |   / _ \ / __/ _` | | |_ / _ \| '__/ _` |/ _ \
| |__| (_) | (_| (_| | |  _| (_) | | | (_| |  __/
|_____\___/ \___\__,_|_|_|  \___/|_|  \__, |\___|
                                       |___/
"""


def print_banner(version: str = "", model: str = "") -> None:
    """Print ASCII art banner with optional version and model info."""
    subtitle_parts: list[str] = []
    if version:
        subtitle_parts.append(f"v{version}")
    if model:
        subtitle_parts.append(f"model: {model}")
    subtitle = "  ".join(subtitle_parts)

    console.print(
        Panel(
            f"[bold cyan]{_BANNER.strip()}[/bold cyan]\n[dim]{subtitle}[/dim]",
            border_style="cyan",
            expand=False,
        )
    )


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

_STATUS_STYLE = {
    "PENDING": "yellow",
    "IN_PROGRESS": "cyan",
    "COMPLETED": "green",
    "FAILED": "red",
    "SKIPPED": "dim",
}


def print_plan(plan: AgentPlan) -> None:
    """Display an :class:`AgentPlan` as a Rich table."""
    table = Table(title="Execution Plan", show_lines=True)
    table.add_column("#", style="bold", width=5)
    table.add_column("Description")
    table.add_column("Files")
    table.add_column("Op", width=8)
    table.add_column("Status", width=12)

    for step in plan.steps:
        style = _STATUS_STYLE.get(step.status.value, "")
        table.add_row(
            str(step.step_id),
            step.description,
            "\n".join(step.files_involved) if step.files_involved else "—",
            step.operation.value,
            f"[{style}]{step.status.value}[/{style}]",
        )

    if plan.reasoning:
        console.print(f"\n[dim]Reasoning: {plan.reasoning}[/dim]")
    console.print(table)


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


def print_chunks(chunks: list[FileChunk]) -> None:
    """Print retrieved chunks grouped by file with syntax highlighting."""
    if not chunks:
        console.print("[dim]No chunks retrieved.[/dim]")
        return

    # Group by file_path
    grouped: dict[str, list[FileChunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk.file_path, []).append(chunk)

    for file_path, file_chunks in grouped.items():
        # Sort by start_line
        file_chunks.sort(key=lambda c: c.start_line)
        for chunk in file_chunks:
            # Guess lexer from extension
            ext = file_path.rsplit(".", 1)[-1] if "." in file_path else "text"
            lexer_map = {
                "py": "python", "js": "javascript", "ts": "typescript",
                "tsx": "typescript", "jsx": "javascript", "go": "go",
                "rs": "rust", "java": "java", "yml": "yaml", "yaml": "yaml",
                "json": "json", "toml": "toml", "md": "markdown",
                "sh": "bash", "rb": "ruby",
            }
            lexer = lexer_map.get(ext, "text")

            syntax = Syntax(
                chunk.content,
                lexer,
                theme="monokai",
                line_numbers=True,
                start_line=chunk.start_line,
            )
            title = f"{file_path}  L{chunk.start_line}–{chunk.end_line}  (score: {chunk.score:.2f})"
            console.print(Panel(syntax, title=title, border_style="blue", expand=True))


# ---------------------------------------------------------------------------
# Verification results
# ---------------------------------------------------------------------------


def print_verification_results(results: list[VerificationResult]) -> None:
    """Show verification results in a styled table."""
    if not results:
        console.print("[dim]No verification results.[/dim]")
        return

    table = Table(title="Verification Results", show_lines=True)
    table.add_column("Command", style="bold")
    table.add_column("Status", width=8)
    table.add_column("Errors", width=8, justify="right")
    table.add_column("Warnings", width=10, justify="right")
    table.add_column("Details")

    for r in results:
        status = "[bold green]PASS[/bold green]" if r.success else "[bold red]FAIL[/bold red]"
        details = ""
        if not r.success:
            # Show first few lines of stderr or stdout
            output = (r.stderr or r.stdout).strip()
            if output:
                lines = output.splitlines()
                details = "\n".join(lines[:5])
                if len(lines) > 5:
                    details += f"\n… ({len(lines) - 5} more lines)"

        table.add_row(
            r.command,
            status,
            str(r.error_count),
            str(r.warning_count),
            details,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def print_diff(diff_str: str) -> None:
    """Pretty-print a unified diff string."""
    if not diff_str.strip():
        console.print("[dim]No differences.[/dim]")
        return

    syntax = Syntax(diff_str, "diff", theme="monokai", line_numbers=False)
    console.print(Panel(syntax, border_style="cyan", expand=True))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(state: AgentState) -> None:
    """Show a final summary panel for an agent run."""
    lines: list[str] = [
        f"[bold]Task:[/bold] {state.task}",
        f"[bold]Phase:[/bold] {state.phase.value}",
        f"[bold]Iterations:[/bold] {state.iteration}",
        f"[bold]Patches applied:[/bold] {len(state.patches_applied)}",
        f"[bold]Verification runs:[/bold] {len(state.verification_results)}",
    ]

    if state.completed:
        lines.append("[bold green]Status: COMPLETED[/bold green]")
    elif state.error:
        lines.append(f"[bold red]Error: {state.error}[/bold red]")

    if state.summary:
        lines.append(f"\n{state.summary}")

    border = "green" if state.completed else "red" if state.error else "yellow"
    console.print(
        Panel(
            "\n".join(lines),
            title="[bold]Task Summary[/bold]",
            border_style=border,
            expand=False,
        )
    )


# ---------------------------------------------------------------------------
# Patch confirmation
# ---------------------------------------------------------------------------


def confirm_patch(op: PatchOperation) -> bool:
    """Interactively ask the user whether to apply a patch."""
    console.print(f"\n[bold]{op.operation_type.value}[/bold] {op.file_path}")
    if op.description:
        console.print(f"  [dim]{op.description}[/dim]")

    if op.diff:
        syntax = Syntax(op.diff, "diff", theme="monokai", line_numbers=False)
        console.print(Panel(syntax, border_style="cyan", expand=True))

    answer = console.input("[bold cyan]Apply this patch? [y/N] [/bold cyan]").strip().lower()
    return answer in ("y", "yes")
