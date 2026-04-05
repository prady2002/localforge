"""Smoke tests for the patching subsystem — proper pytest format."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from localforge.core.config import LocalForgeConfig
from localforge.core.models import OperationType, PatchOperation
from localforge.patching.patcher import FilePatcher
from localforge.patching.validator import PatchValidator


@pytest.fixture()
def patcher(tmp_path: Path) -> FilePatcher:
    cfg = LocalForgeConfig(repo_path=str(tmp_path), auto_approve=True)
    return FilePatcher(tmp_path, cfg)


@pytest.fixture()
def validator() -> PatchValidator:
    return PatchValidator()


def test_create_parse_and_apply(patcher: FilePatcher, tmp_path: Path) -> None:
    resp = json.dumps({
        "file_path": "src/hello.py",
        "operation": "CREATE",
        "description": "create hello module",
        "full_content": "def hello():\n    return 'world'\n",
    })
    op = patcher.parse_patch_response(resp)
    assert op.operation_type == OperationType.CREATE
    assert op.new_content == "def hello():\n    return 'world'\n"
    assert patcher.apply_patch(op)
    assert (tmp_path / "src" / "hello.py").read_text() == "def hello():\n    return 'world'\n"


def test_modify_exact_match(patcher: FilePatcher, tmp_path: Path) -> None:
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "hello.py").write_text(
        "def hello():\n    return 'world'\n", encoding="utf-8"
    )
    resp = json.dumps({
        "file_path": "src/hello.py",
        "operation": "MODIFY",
        "description": "change return value",
        "search_block": "return 'world'",
        "replace_block": "return 'universe'",
    })
    op = patcher.parse_patch_response(resp)
    assert op.operation_type == OperationType.MODIFY
    assert "universe" in op.new_content
    assert patcher.apply_patch(op)
    assert "universe" in (tmp_path / "src" / "hello.py").read_text()


def test_modify_fuzzy_match(patcher: FilePatcher, tmp_path: Path) -> None:
    (tmp_path / "fuzzy.py").write_text(
        "def process(x):\n    result = x + 1\n    return result\n", encoding="utf-8"
    )
    resp = json.dumps({
        "file_path": "fuzzy.py",
        "operation": "MODIFY",
        "description": "fix process",
        "search_block": "def process(x):\n    result = x +1\n    return result",
        "replace_block": "def process(x):\n    result = x + 2\n    return result",
    })
    op = patcher.parse_patch_response(resp)
    assert "x + 2" in op.new_content


def test_delete_operation(patcher: FilePatcher, tmp_path: Path) -> None:
    (tmp_path / "to_delete.txt").write_text("bye", encoding="utf-8")
    resp = json.dumps({
        "file_path": "to_delete.txt",
        "operation": "DELETE",
        "description": "remove temp file",
    })
    op = patcher.parse_patch_response(resp)
    assert op.operation_type == OperationType.DELETE
    assert patcher.apply_patch(op)
    assert not (tmp_path / "to_delete.txt").exists()


def test_backup_and_rollback(patcher: FilePatcher, tmp_path: Path) -> None:
    target = tmp_path / "roll.py"
    target.write_text("original", encoding="utf-8")
    op = PatchOperation(
        file_path="roll.py",
        operation_type=OperationType.MODIFY,
        original_content="original",
        new_content="modified",
        diff="",
        description="test",
    )
    patcher.apply_patch(op)
    backups = list((tmp_path / ".localforge" / "backups").iterdir())
    assert len(backups) >= 1
    ts = backups[0].name
    target.write_text("OVERWRITTEN", encoding="utf-8")
    assert patcher.rollback(ts)
    assert "OVERWRITTEN" not in target.read_text(encoding="utf-8")


def test_generate_diff(patcher: FilePatcher) -> None:
    diff = patcher.generate_diff("a=1\n", "a=2\n", "test.py")
    assert "--- a/test.py" in diff
    assert "+++ b/test.py" in diff


def test_find_fuzzy_exact(patcher: FilePatcher) -> None:
    s, e = patcher.find_fuzzy("hello world foo", "hello world foo", 0.9)
    assert s == 0 and e == 15


def test_find_fuzzy_no_match(patcher: FilePatcher) -> None:
    s, e = patcher.find_fuzzy("hello world foo", "completely different text", 0.9)
    assert s == -1 and e == -1


def test_validate_syntax_python(validator: PatchValidator) -> None:
    ok, err = validator.validate_syntax("a.py", "def f(): pass")
    assert ok and err == ""
    ok, err = validator.validate_syntax("a.py", "def f( pass")
    assert not ok and err != ""


def test_validate_syntax_json(validator: PatchValidator) -> None:
    ok, _ = validator.validate_syntax("a.json", '{"a": 1}')
    assert ok
    ok, _ = validator.validate_syntax("a.json", "{bad}")
    assert not ok


def test_validate_syntax_yaml(validator: PatchValidator) -> None:
    ok, _ = validator.validate_syntax("a.yaml", "key: value")
    assert ok


def test_validate_syntax_unknown(validator: PatchValidator) -> None:
    ok, _ = validator.validate_syntax("a.txt", "anything")
    assert ok


def test_validate_patch_safety_clean(validator: PatchValidator) -> None:
    op = PatchOperation(
        file_path="a.py", operation_type=OperationType.MODIFY,
        new_content="x = 1", diff="", description="",
    )
    ok, warns = validator.validate_patch_safety(op)
    assert ok and len(warns) == 0


def test_validate_patch_safety_dangerous(validator: PatchValidator) -> None:
    op = PatchOperation(
        file_path="a.py", operation_type=OperationType.MODIFY,
        new_content='import os; os.system("rm -rf /")\neval(input())\npassword="secret123"',
        diff="", description="",
    )
    ok, warns = validator.validate_patch_safety(op)
    assert not ok
    assert len(warns) >= 3


def test_validate_patch_safety_delete(validator: PatchValidator) -> None:
    op = PatchOperation(
        file_path="a.py", operation_type=OperationType.DELETE,
        new_content=None, diff="", description="",
    )
    ok, warns = validator.validate_patch_safety(op)
    assert ok
