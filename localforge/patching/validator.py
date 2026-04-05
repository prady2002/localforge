"""Patch validation utilities for localforge."""

from __future__ import annotations

import ast
import json
import re

from localforge.core.models import OperationType, PatchOperation

# Patterns that may indicate dangerous operations
_DANGEROUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Recursive deletion (rm -rf)", re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b")),
    ("Recursive deletion (shutil.rmtree)", re.compile(r"\bshutil\.rmtree\b")),
    ("File deletion (os.remove)", re.compile(r"\bos\.remove\b")),
    ("File deletion (os.unlink)", re.compile(r"\bos\.unlink\b")),
    ("Code execution via eval()", re.compile(r"\beval\s*\(")),
    ("Code execution via exec()", re.compile(r"\bexec\s*\(")),
    (
        "Potential hardcoded password",
        re.compile(
            r"""(?i)(?:password|passwd|pwd)\s*=\s*['"][^'"]{4,}['"]"""
        ),
    ),
    (
        "Potential hardcoded secret/token",
        re.compile(
            r"""(?i)(?:secret|token|api_key|apikey)\s*=\s*['"][^'"]{4,}['"]"""
        ),
    ),
    ("Subprocess shell=True", re.compile(r"\bsubprocess\.\w+\(.*shell\s*=\s*True")),
    ("os.system call", re.compile(r"\bos\.system\s*\(")),
]


class PatchValidator:
    """Validates patch content for syntax correctness and safety."""

    def validate_syntax(self, file_path: str, content: str) -> tuple[bool, str]:
        """Check that *content* is syntactically valid for the given file type.

        Returns ``(is_valid, error_message)``.  *error_message* is empty when
        the content is valid.
        """
        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""

        if ext == "py":
            return self._validate_python(content)
        if ext == "json":
            return self._validate_json(content)
        if ext in ("yaml", "yml"):
            return self._validate_yaml(content)

        # No validator for this file type – assume OK
        return (True, "")

    def validate_patch_safety(
        self, op: PatchOperation
    ) -> tuple[bool, list[str]]:
        """Scan the patch for dangerous patterns.

        Returns ``(is_safe, warnings)``.  *is_safe* is ``False`` when at
        least one warning is raised.
        """
        warnings: list[str] = []

        # Only inspect content that the patch introduces
        text_to_check = op.new_content or ""
        if op.operation_type == OperationType.DELETE:
            # Nothing dangerous about the deletion intent itself beyond the
            # file being removed, which is expected.
            return (True, [])

        for label, pattern in _DANGEROUS_PATTERNS:
            if pattern.search(text_to_check):
                warnings.append(label)

        return (len(warnings) == 0, warnings)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_python(content: str) -> tuple[bool, str]:
        try:
            ast.parse(content)
            return (True, "")
        except SyntaxError as exc:
            msg = f"line {exc.lineno}: {exc.msg}" if exc.lineno else str(exc)
            return (False, msg)

    @staticmethod
    def _validate_json(content: str) -> tuple[bool, str]:
        try:
            json.loads(content)
            return (True, "")
        except json.JSONDecodeError as exc:
            return (False, str(exc))

    @staticmethod
    def _validate_yaml(content: str) -> tuple[bool, str]:
        try:
            import yaml  # noqa: PLC0415

            yaml.safe_load(content)
            return (True, "")
        except Exception as exc:  # noqa: BLE001
            return (False, str(exc))
