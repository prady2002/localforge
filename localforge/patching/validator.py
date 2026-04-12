"""Patch validation utilities for localforge.

Supports syntax validation for Python, JSON, YAML, JavaScript, TypeScript,
Go, Rust, Java, C/C++, Ruby, PHP, and more.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import shutil

from localforge.core.models import OperationType, PatchOperation

# Patterns that may indicate dangerous operations (multi-language)
_DANGEROUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Deletion patterns
    ("Recursive deletion (rm -rf)", re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b")),
    ("Recursive deletion (shutil.rmtree)", re.compile(r"\bshutil\.rmtree\b")),
    ("File deletion (os.remove)", re.compile(r"\bos\.remove\b")),
    ("File deletion (os.unlink)", re.compile(r"\bos\.unlink\b")),
    ("File deletion (fs.rmSync)", re.compile(r"\bfs\.(rmSync|unlinkSync|rmdirSync)\b")),
    # Code execution
    ("Code execution via eval()", re.compile(r"\beval\s*\(")),
    ("Code execution via exec()", re.compile(r"\bexec\s*\(")),
    ("Code execution via Function()", re.compile(r"\bnew\s+Function\s*\(")),
    ("Unsafe deserialization", re.compile(r"\b(pickle\.loads?|yaml\.unsafe_load|Marshal\.load)\s*\(")),
    # Secrets
    (
        "Potential hardcoded password",
        re.compile(
            r"""(?i)(?:password|passwd|pwd)\s*=\s*['"][^'"]{4,}['"]"""
        ),
    ),
    (
        "Potential hardcoded secret/token",
        re.compile(
            r"""(?i)(?:secret|token|api_key|apikey|private_key)\s*=\s*['"][^'"]{4,}['"]"""
        ),
    ),
    # Shell injection
    ("Subprocess shell=True", re.compile(r"\bsubprocess\.\w+\(.*shell\s*=\s*True")),
    ("os.system call", re.compile(r"\bos\.system\s*\(")),
    ("child_process.exec", re.compile(r"\bchild_process\.(exec|execSync)\s*\(")),
    ("Runtime.exec (Java)", re.compile(r"\bRuntime\.getRuntime\(\)\.exec\s*\(")),
    # SQL injection
    ("Potential SQL injection (string concatenation)", re.compile(r"""(?i)(?:execute|query)\s*\(\s*['"][^'"]*['"]\s*\+""")),
]


class PatchValidator:
    """Validates patch content for syntax correctness and safety."""

    def validate_syntax(self, file_path: str, content: str) -> tuple[bool, str]:
        """Check that *content* is syntactically valid for the given file type.

        Supports: Python, JSON, YAML, JavaScript, TypeScript, Go, Ruby, PHP,
        and structural validation for Java, C/C++, Kotlin, Swift, Rust, CSS.

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
        if ext in ("js", "mjs", "cjs"):
            return self._validate_javascript(content, file_path)
        if ext in ("ts", "tsx", "mts"):
            return self._validate_typescript(content, file_path)
        if ext == "go":
            return self._validate_go(content, file_path)
        if ext == "rb":
            return self._validate_ruby(content, file_path)
        if ext == "php":
            return self._validate_php(content, file_path)
        if ext in ("java", "kt", "kts", "rs", "c", "h", "cpp", "hpp",
                    "cc", "cxx", "swift", "css", "scss", "less"):
            return self._validate_braces(content, ext)
        if ext in ("xml", "html", "htm", "xhtml", "svg"):
            return self._validate_xml(content)

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

    @staticmethod
    def _validate_javascript(content: str, file_path: str) -> tuple[bool, str]:
        """Validate JavaScript syntax via Node.js --check if available."""
        if not shutil.which("node"):
            return PatchValidator._validate_braces(content, "js")
        return PatchValidator._validate_with_external(
            content, file_path, ["node", "--check"], "JavaScript"
        )

    @staticmethod
    def _validate_typescript(content: str, file_path: str) -> tuple[bool, str]:
        """Validate TypeScript via npx tsc if available, else braces check."""
        if not shutil.which("npx"):
            return PatchValidator._validate_braces(content, "ts")
        return PatchValidator._validate_with_external(
            content, file_path, ["npx", "tsc", "--noEmit", "--allowJs"], "TypeScript"
        )

    @staticmethod
    def _validate_go(content: str, file_path: str) -> tuple[bool, str]:
        """Validate Go syntax via gofmt if available."""
        if not shutil.which("gofmt"):
            return PatchValidator._validate_braces(content, "go")
        return PatchValidator._validate_with_external(
            content, file_path, ["gofmt", "-e"], "Go"
        )

    @staticmethod
    def _validate_ruby(content: str, file_path: str) -> tuple[bool, str]:
        """Validate Ruby syntax via ruby -c if available."""
        if not shutil.which("ruby"):
            return (True, "")
        return PatchValidator._validate_with_external(
            content, file_path, ["ruby", "-c"], "Ruby"
        )

    @staticmethod
    def _validate_php(content: str, file_path: str) -> tuple[bool, str]:
        """Validate PHP syntax via php -l if available."""
        if not shutil.which("php"):
            return (True, "")
        return PatchValidator._validate_with_external(
            content, file_path, ["php", "-l"], "PHP"
        )

    @staticmethod
    def _validate_xml(content: str) -> tuple[bool, str]:
        """Validate XML/HTML well-formedness."""
        try:
            import xml.etree.ElementTree as ET
            ET.fromstring(content)
            return (True, "")
        except Exception as exc:
            return (False, f"XML error: {exc}")

    @staticmethod
    def _validate_braces(content: str, ext: str) -> tuple[bool, str]:
        """Validate brace/bracket/paren balance for C-like languages."""
        stack: list[tuple[str, int]] = []
        openers = {"(": ")", "[": "]", "{": "}"}
        closers = {")": "(", "]": "[", "}": "{"}
        in_string = False
        string_char = ""
        escape_next = False
        in_line_comment = False
        in_block_comment = False
        line_num = 1

        for i, ch in enumerate(content):
            if ch == "\n":
                line_num += 1
                in_line_comment = False
                continue
            if escape_next:
                escape_next = False
                continue
            if in_block_comment:
                if ch == "*" and i + 1 < len(content) and content[i + 1] == "/":
                    in_block_comment = False
                continue
            if in_line_comment:
                continue
            if in_string:
                if ch == "\\":
                    escape_next = True
                elif ch == string_char:
                    in_string = False
                continue
            if ch == "/" and i + 1 < len(content):
                next_ch = content[i + 1]
                if next_ch == "/":
                    in_line_comment = True
                    continue
                elif next_ch == "*":
                    in_block_comment = True
                    continue
            if ch in ('"', "'", "`"):
                in_string = True
                string_char = ch
                continue
            if ch in openers:
                stack.append((ch, line_num))
            elif ch in closers:
                if not stack:
                    return (False, f"Unexpected '{ch}' at line {line_num}")
                top, _ = stack[-1]
                if closers[ch] != top:
                    return (False, f"Mismatched '{ch}' at line {line_num}")
                stack.pop()

        if stack:
            return (False, f"Unclosed '{stack[-1][0]}' at line {stack[-1][1]}")
        return (True, "")

    @staticmethod
    def _validate_with_external(
        content: str, file_path: str, cmd_prefix: list[str], lang: str,
    ) -> tuple[bool, str]:
        """Validate syntax using an external tool with a temp file."""
        import os
        import tempfile
        from pathlib import Path

        suffix = Path(file_path).suffix
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=suffix, delete=False, encoding="utf-8",
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name

            result = subprocess.run(
                cmd_prefix + [tmp_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                return (False, f"{lang} syntax error: {err[:200]}")
            return (True, "")
        except (subprocess.TimeoutExpired, OSError):
            return (True, "")  # Can't validate — allow
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
