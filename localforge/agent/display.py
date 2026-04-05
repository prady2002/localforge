"""Rich display helpers for the orchestrator."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from localforge.core.models import AgentPlan, MultiAgentState, PatchOperation, PlanStep

console = Console()


class OrchestratorDisplay:
    """Pretty-prints orchestrator progress using Rich."""

    # ------------------------------------------------------------------
    # Phase & step banners
    # ------------------------------------------------------------------

    def phase(self, name: str, description: str) -> None:
        console.print(f"\n[bold cyan]>> PHASE: {name}[/bold cyan] — {description}")

    def step(self, step: PlanStep, attempt: int) -> None:
        console.print(
            f"  [bold]-> Step {step.step_id}:[/bold] {step.description} "
            f"[dim](attempt {attempt})[/dim]"
        )

    def step_success(self, step: PlanStep) -> None:
        console.print(f"  [bold green][OK] Step {step.step_id} complete[/bold green]")

    def step_failed(self, step: PlanStep) -> None:
        console.print(
            f"  [bold red][FAIL] Step {step.step_id} failed after all retries[/bold red]"
        )

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------

    def show_plan(self, plan: AgentPlan) -> None:
        table = Table(title="Execution Plan", show_lines=True)
        table.add_column("Step #", style="bold", width=7)
        table.add_column("Description")
        table.add_column("Files")
        table.add_column("Operation", width=10)
        table.add_column("Status", width=12)

        status_style = {
            "PENDING": "yellow",
            "IN_PROGRESS": "cyan",
            "COMPLETED": "green",
            "FAILED": "red",
            "SKIPPED": "dim",
        }

        for s in plan.steps:
            style = status_style.get(s.status.value, "")
            table.add_row(
                str(s.step_id),
                s.description,
                "\n".join(s.files_involved) if s.files_involved else "—",
                s.operation.value,
                f"[{style}]{s.status.value}[/{style}]",
            )
        console.print(table)

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def warning(self, msg: str) -> None:
        console.print(f"[bold yellow][WARN] {msg}[/bold yellow]")

    def error(self, msg: str) -> None:
        console.print(f"[bold red][FAIL] ERROR: {msg}[/bold red]")

    # ------------------------------------------------------------------
    # Patch confirmation
    # ------------------------------------------------------------------

    def confirm_patch(self, op: PatchOperation) -> bool:
        console.print(
            f"\n[bold]Patch:[/bold] {op.operation_type.value} {op.file_path}"
        )
        if op.description:
            console.print(f"  {op.description}")
        answer = console.input("[bold cyan]Apply this patch? [y/N] [/bold cyan]").strip().lower()
        return answer in ("y", "yes")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def show_summary(self, state: MultiAgentState) -> None:
        body_lines = [
            f"[bold]Task:[/bold] {state.task}",
            f"[bold]Iterations:[/bold] {state.iteration}",
            f"[bold]Messages exchanged:[/bold] {len(state.messages)}",
        ]

        # Pull key_changes from the last summarizer message, if available
        for msg in reversed(state.messages):
            if msg.role.value == "SUMMARIZER" and msg.structured_data:
                summary_text = msg.structured_data.get("summary", "")
                if summary_text:
                    body_lines.append(f"\n{summary_text}")
                for change in msg.structured_data.get("key_changes", []):
                    body_lines.append(f"  • {change}")
                break

        panel = Panel(
            "\n".join(body_lines),
            title="[bold green]Task Summary[/bold green]",
            border_style="green",
            expand=False,
        )
        console.print(panel)
