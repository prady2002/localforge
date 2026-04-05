"""File patching system for localforge."""

from __future__ import annotations

import difflib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from localforge.core.config import LocalForgeConfig
from localforge.core.models import OperationType, PatchOperation

console = Console()


class FilePatcher:
    """Applies, previews, and rolls back file patches produced by the agent."""

    def __init__(self, repo_path: Path, config: LocalForgeConfig) -> None:
        self.repo_path = repo_path.resolve()
        self.config = config
        self._backup_root = self.repo_path / ".localforge" / "backups"

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_patch_response(self, response: str) -> PatchOperation:
        """Parse model JSON output into a ``PatchOperation``."""
        data = json.loads(response)
        file_path: str = data["file_path"]
        operation = OperationType(data["operation"])
        description: str = data.get("description", "")

        abs_path = (self.repo_path / file_path).resolve()

        # Guard against path traversal – the resolved path must stay inside repo
        if not abs_path.is_relative_to(self.repo_path):
            raise ValueError(
                f"Path traversal blocked: {file_path!r} resolves outside the repository"
            )

        if operation == OperationType.CREATE:
            full_content = data.get("full_content", "")
            return PatchOperation(
                file_path=file_path,
                operation_type=operation,
                original_content=None,
                new_content=full_content,
                diff=self.generate_diff("", full_content, file_path),
                description=description,
            )

        if operation == OperationType.MODIFY:
            if not abs_path.is_file():
                raise FileNotFoundError(f"Cannot MODIFY: file not found: {file_path}")

            original = abs_path.read_text(encoding="utf-8")
            search_block: str = data["search_block"]
            replace_block: str = data["replace_block"]

            # Try exact match first, then fuzzy
            if search_block in original:
                new_content = original.replace(search_block, replace_block, 1)
            else:
                start, end = self.find_fuzzy(original, search_block)
                if start == -1:
                    raise ValueError(
                        f"search_block not found in {file_path} "
                        "(exact match failed and fuzzy match below threshold)"
                    )
                new_content = original[:start] + replace_block + original[end:]

            return PatchOperation(
                file_path=file_path,
                operation_type=operation,
                original_content=original,
                new_content=new_content,
                diff=self.generate_diff(original, new_content, file_path),
                description=description,
            )

        if operation == OperationType.DELETE:
            original = abs_path.read_text(encoding="utf-8") if abs_path.is_file() else ""
            return PatchOperation(
                file_path=file_path,
                operation_type=operation,
                original_content=original,
                new_content=None,
                diff=self.generate_diff(original, "", file_path),
                description=description,
            )

        raise ValueError(f"Unknown operation: {data.get('operation')}")

    # ------------------------------------------------------------------
    # Applying
    # ------------------------------------------------------------------

    def apply_patch(self, op: PatchOperation) -> bool:
        """Apply a ``PatchOperation`` to disk.  Returns ``True`` on success."""
        from localforge.patching.validator import PatchValidator

        abs_path = self.repo_path / op.file_path
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")

        # Safety check
        validator = PatchValidator()
        is_safe, warnings = validator.validate_patch_safety(op)
        if not is_safe:
            console.print(f"[bold yellow]Safety warnings for {op.file_path}:[/bold yellow]")
            for w in warnings:
                console.print(f"  [yellow]⚠ {w}[/yellow]")
            if not self.config.auto_approve:
                answer = console.input("[bold]Apply anyway? [y/N] [/bold]").strip().lower()
                if answer not in ("y", "yes"):
                    return False

        # Syntax validation for MODIFY and CREATE
        if op.operation_type in (OperationType.MODIFY, OperationType.CREATE) and op.new_content:
            is_valid, error = validator.validate_syntax(op.file_path, op.new_content)
            if not is_valid:
                console.print(
                    f"[bold yellow]Syntax warning for {op.file_path}: {error}[/bold yellow]"
                )

        # Backup existing file
        if abs_path.is_file():
            self._backup_file(abs_path, op.file_path, timestamp)

        if op.operation_type == OperationType.CREATE:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(op.new_content or "", encoding="utf-8")
            return True

        if op.operation_type == OperationType.MODIFY:
            if op.new_content is None:
                return False
            abs_path.write_text(op.new_content, encoding="utf-8")
            return True

        if op.operation_type == OperationType.DELETE:
            if abs_path.is_file():
                abs_path.unlink()
            return True

        return False

    # ------------------------------------------------------------------
    # Diff helpers
    # ------------------------------------------------------------------

    def generate_diff(self, original: str, modified: str, file_path: str) -> str:
        """Return a unified-diff string for *original* → *modified*."""
        orig_lines = original.splitlines(keepends=True)
        mod_lines = modified.splitlines(keepends=True)
        diff_lines = difflib.unified_diff(
            orig_lines,
            mod_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        )
        return "".join(diff_lines)

    def show_diff(self, op: PatchOperation) -> None:
        """Print a Rich Syntax-highlighted diff panel."""
        diff_text = op.diff or self.generate_diff(
            op.original_content or "",
            op.new_content or "",
            op.file_path,
        )
        if not diff_text:
            console.print(f"[dim]No diff for {op.file_path}[/dim]")
            return

        syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=True)
        title = f"{op.operation_type.value} {op.file_path}"
        console.print(Panel(syntax, title=title, border_style="cyan"))

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def rollback(self, backup_timestamp: str) -> bool:
        """Restore all files from a specific backup timestamp."""
        backup_dir = self._backup_root / backup_timestamp
        if not backup_dir.is_dir():
            console.print(f"[red]Backup not found: {backup_timestamp}[/red]")
            return False

        for backup_file in backup_dir.rglob("*"):
            if backup_file.is_file():
                rel = backup_file.relative_to(backup_dir)
                target = self.repo_path / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_file, target)

        console.print(f"[green]Rolled back to backup {backup_timestamp}[/green]")
        return True

    # ------------------------------------------------------------------
    # Fuzzy matching
    # ------------------------------------------------------------------

    def find_fuzzy(
        self, content: str, search_block: str, threshold: float = 0.9
    ) -> tuple[int, int]:
        """Find approximate location of *search_block* in *content*.

        Returns ``(start_idx, end_idx)`` in *content*, or ``(-1, -1)`` if the
        best match ratio is below *threshold*.
        """
        search_len = len(search_block)
        if search_len == 0 or not content:
            return (-1, -1)

        best_ratio = 0.0
        best_start = -1
        best_end = -1

        # Slide a window of similar size over content
        for window_pad in range(max(1, search_len // 5)):
            for wlen in (search_len - window_pad, search_len + window_pad):
                if wlen <= 0 or wlen > len(content):
                    continue
                for start in range(len(content) - wlen + 1):
                    candidate = content[start : start + wlen]
                    ratio = difflib.SequenceMatcher(
                        None, search_block, candidate
                    ).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_start = start
                        best_end = start + wlen
                    if ratio >= threshold:
                        return (best_start, best_end)

        if best_ratio >= threshold:
            return (best_start, best_end)
        return (-1, -1)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _backup_file(self, abs_path: Path, rel_path: str, timestamp: str) -> None:
        """Copy *abs_path* into the backup directory under *timestamp*."""
        dest = self._backup_root / timestamp / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(abs_path, dest)
