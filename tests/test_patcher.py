"""Tests for localforge.patching — FilePatcher and PatchValidator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from localforge.core.config import LocalForgeConfig
from localforge.core.models import OperationType, PatchOperation
from localforge.patching.patcher import FilePatcher
from localforge.patching.validator import PatchValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patcher(tmp_path: Path) -> FilePatcher:
    cfg = LocalForgeConfig(repo_path=str(tmp_path), auto_approve=True)
    return FilePatcher(tmp_path, cfg)


# ---------------------------------------------------------------------------
# test_parse_patch_response
# ---------------------------------------------------------------------------


def test_parse_patch_response_modify(tmp_path: Path) -> None:
    """parse_patch_response should handle a valid MODIFY JSON payload."""
    patcher = _patcher(tmp_path)

    # Create the file that will be modified
    target = tmp_path / "calc.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    response = json.dumps({
        "file_path": "calc.py",
        "operation": "MODIFY",
        "search_block": "return a - b",
        "replace_block": "return a + b",
        "description": "Fix add function",
    })

    op = patcher.parse_patch_response(response)

    assert op.file_path == "calc.py"
    assert op.operation_type == OperationType.MODIFY
    assert "return a + b" in op.new_content
    assert op.diff  # non-empty diff


def test_parse_patch_response_create(tmp_path: Path) -> None:
    """parse_patch_response should handle a CREATE operation."""
    patcher = _patcher(tmp_path)
    response = json.dumps({
        "file_path": "new_file.py",
        "operation": "CREATE",
        "full_content": "print('hello')\n",
        "description": "Create new file",
    })

    op = patcher.parse_patch_response(response)
    assert op.operation_type == OperationType.CREATE
    assert op.new_content == "print('hello')\n"


# ---------------------------------------------------------------------------
# test_apply_modify
# ---------------------------------------------------------------------------


def test_apply_modify(tmp_path: Path) -> None:
    """apply_patch(MODIFY) should change file content on disk."""
    patcher = _patcher(tmp_path)
    target = tmp_path / "calc.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    op = PatchOperation(
        file_path="calc.py",
        operation_type=OperationType.MODIFY,
        original_content="def add(a, b):\n    return a - b\n",
        new_content="def add(a, b):\n    return a + b\n",
        diff="",
        description="Fix add",
    )

    result = patcher.apply_patch(op)
    assert result is True
    assert "return a + b" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# test_apply_create
# ---------------------------------------------------------------------------


def test_apply_create(tmp_path: Path) -> None:
    """apply_patch(CREATE) should create a new file on disk."""
    patcher = _patcher(tmp_path)
    target = tmp_path / "brand_new.py"
    assert not target.exists()

    op = PatchOperation(
        file_path="brand_new.py",
        operation_type=OperationType.CREATE,
        original_content=None,
        new_content="x = 42\n",
        diff="",
        description="New file",
    )

    result = patcher.apply_patch(op)
    assert result is True
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "x = 42\n"


# ---------------------------------------------------------------------------
# test_apply_create_nested
# ---------------------------------------------------------------------------


def test_apply_create_nested(tmp_path: Path) -> None:
    """apply_patch(CREATE) should create parent directories as needed."""
    patcher = _patcher(tmp_path)
    op = PatchOperation(
        file_path="deep/nested/dir/file.py",
        operation_type=OperationType.CREATE,
        new_content="pass\n",
        diff="",
        description="Deep create",
    )
    assert patcher.apply_patch(op) is True
    assert (tmp_path / "deep" / "nested" / "dir" / "file.py").exists()


# ---------------------------------------------------------------------------
# test_backup_created
# ---------------------------------------------------------------------------


def test_backup_created(tmp_path: Path) -> None:
    """Applying a MODIFY patch should create a backup of the original file."""
    patcher = _patcher(tmp_path)
    target = tmp_path / "data.py"
    target.write_text("old content", encoding="utf-8")

    op = PatchOperation(
        file_path="data.py",
        operation_type=OperationType.MODIFY,
        original_content="old content",
        new_content="new content",
        diff="",
        description="Update",
    )

    patcher.apply_patch(op)

    backup_root = tmp_path / ".localforge" / "backups"
    assert backup_root.exists()

    # At least one backup timestamp directory should exist
    backup_dirs = list(backup_root.iterdir())
    assert len(backup_dirs) >= 1

    # The backed-up file should contain the original content
    backup_files = list(backup_dirs[0].rglob("data.py"))
    assert len(backup_files) == 1
    assert backup_files[0].read_text(encoding="utf-8") == "old content"


# ---------------------------------------------------------------------------
# test_rollback
# ---------------------------------------------------------------------------


def test_rollback(tmp_path: Path) -> None:
    """rollback() should restore original content from backup."""
    patcher = _patcher(tmp_path)
    target = tmp_path / "restore_me.py"
    target.write_text("original", encoding="utf-8")

    op = PatchOperation(
        file_path="restore_me.py",
        operation_type=OperationType.MODIFY,
        original_content="original",
        new_content="modified",
        diff="",
        description="Will be rolled back",
    )
    patcher.apply_patch(op)
    assert target.read_text(encoding="utf-8") == "modified"

    # Find the backup timestamp
    backup_root = tmp_path / ".localforge" / "backups"
    timestamps = [d.name for d in backup_root.iterdir() if d.is_dir()]
    assert len(timestamps) >= 1

    result = patcher.rollback(timestamps[0])
    assert result is True
    assert target.read_text(encoding="utf-8") == "original"


# ---------------------------------------------------------------------------
# test_fuzzy_find
# ---------------------------------------------------------------------------


def test_fuzzy_find_exact(tmp_path: Path) -> None:
    """find_fuzzy should find an exact substring."""
    patcher = _patcher(tmp_path)
    content = "def foo():\n    return 42\n\ndef bar():\n    return 99\n"
    start, end = patcher.find_fuzzy(content, "return 42")
    assert start != -1
    assert content[start:end] == "return 42"


def test_fuzzy_find_approximate(tmp_path: Path) -> None:
    """find_fuzzy should find a near-match above threshold."""
    patcher = _patcher(tmp_path)
    content = "def foo():\n    return 42\n"
    # Slightly different from actual — one char off
    start, end = patcher.find_fuzzy(content, "return 43", threshold=0.7)
    # With a lenient threshold it should find something
    assert start != -1


def test_fuzzy_find_no_match(tmp_path: Path) -> None:
    """find_fuzzy should return (-1, -1) when nothing is close enough."""
    patcher = _patcher(tmp_path)
    content = "def foo():\n    return 42\n"
    start, end = patcher.find_fuzzy(content, "completely unrelated gibberish xyz 9999")
    assert (start, end) == (-1, -1)


# ---------------------------------------------------------------------------
# test_syntax_validation
# ---------------------------------------------------------------------------


def test_syntax_validation_valid_python() -> None:
    """Valid Python should pass syntax validation."""
    validator = PatchValidator()
    ok, msg = validator.validate_syntax("test.py", "def foo():\n    return 1\n")
    assert ok is True
    assert msg == ""


def test_syntax_validation_invalid_python() -> None:
    """Invalid Python should fail syntax validation."""
    validator = PatchValidator()
    ok, msg = validator.validate_syntax("test.py", "def foo(\n    return 1\n")
    assert ok is False
    assert msg  # non-empty error message


def test_syntax_validation_valid_json() -> None:
    validator = PatchValidator()
    ok, msg = validator.validate_syntax("data.json", '{"key": "value"}')
    assert ok is True


def test_syntax_validation_invalid_json() -> None:
    validator = PatchValidator()
    ok, msg = validator.validate_syntax("data.json", '{"key": }')
    assert ok is False


def test_syntax_validation_unknown_extension() -> None:
    """Unknown file types should pass (no validator available)."""
    validator = PatchValidator()
    ok, msg = validator.validate_syntax("readme.txt", "anything goes")
    assert ok is True


# ---------------------------------------------------------------------------
# test_patch_safety
# ---------------------------------------------------------------------------


def test_patch_safety_flags_eval() -> None:
    validator = PatchValidator()
    op = PatchOperation(
        file_path="evil.py",
        operation_type=OperationType.CREATE,
        new_content="result = eval(user_input)",
        diff="",
    )
    safe, warnings = validator.validate_patch_safety(op)
    assert safe is False
    assert any("eval" in w for w in warnings)


def test_patch_safety_clean_code() -> None:
    validator = PatchValidator()
    op = PatchOperation(
        file_path="clean.py",
        operation_type=OperationType.CREATE,
        new_content="def add(a, b):\n    return a + b\n",
        diff="",
    )
    safe, warnings = validator.validate_patch_safety(op)
    assert safe is True
    assert warnings == []


# ---------------------------------------------------------------------------
# Path traversal guard
# ---------------------------------------------------------------------------


def test_path_traversal_blocked(tmp_path: Path) -> None:
    """Ensure that model output with ``../`` path components is rejected."""
    patcher = _patcher(tmp_path)
    payload = json.dumps(
        {
            "file_path": "../../../etc/passwd",
            "operation": "CREATE",
            "full_content": "malicious content",
        }
    )
    with pytest.raises(ValueError, match="Path traversal blocked"):
        patcher.parse_patch_response(payload)
