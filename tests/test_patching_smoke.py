"""Smoke-test for the patching subsystem."""

import json
import shutil
import tempfile
from pathlib import Path

from localforge.patching import FilePatcher, PatchValidator
from localforge.core.config import LocalForgeConfig
from localforge.core.models import PatchOperation, OperationType

cfg = LocalForgeConfig()
tmp = Path(tempfile.mkdtemp())

try:
    print("=== 1. Import check ===")
    print("FilePatcher and PatchValidator imported OK")

    patcher = FilePatcher(tmp, cfg)
    validator = PatchValidator()

    # --- CREATE ---
    print("=== 2. parse + apply CREATE ===")
    resp_create = json.dumps({
        "file_path": "src/hello.py",
        "operation": "CREATE",
        "description": "create hello module",
        "full_content": "def hello():\n    return 'world'\n",
    })
    op_c = patcher.parse_patch_response(resp_create)
    assert op_c.operation_type == OperationType.CREATE
    assert op_c.new_content == "def hello():\n    return 'world'\n"
    assert op_c.original_content is None
    assert "hello.py" in op_c.diff
    ok = patcher.apply_patch(op_c)
    assert ok
    assert (tmp / "src" / "hello.py").read_text() == "def hello():\n    return 'world'\n"
    print("CREATE OK")

    # --- MODIFY ---
    print("=== 3. parse + apply MODIFY (exact match) ===")
    resp_modify = json.dumps({
        "file_path": "src/hello.py",
        "operation": "MODIFY",
        "description": "change return value",
        "search_block": "return 'world'",
        "replace_block": "return 'universe'",
    })
    op_m = patcher.parse_patch_response(resp_modify)
    assert op_m.operation_type == OperationType.MODIFY
    assert "universe" in op_m.new_content
    assert "world" in op_m.original_content
    ok = patcher.apply_patch(op_m)
    assert ok
    assert "universe" in (tmp / "src" / "hello.py").read_text()
    print("MODIFY (exact) OK")

    # --- MODIFY fuzzy ---
    print("=== 4. MODIFY with fuzzy match ===")
    (tmp / "fuzzy.py").write_text("def process(x):\n    result = x + 1\n    return result\n")
    resp_fuzzy = json.dumps({
        "file_path": "fuzzy.py",
        "operation": "MODIFY",
        "description": "fix process",
        "search_block": "def process(x):\n    result = x +1\n    return result",
        "replace_block": "def process(x):\n    result = x + 2\n    return result",
    })
    op_f = patcher.parse_patch_response(resp_fuzzy)
    assert "x + 2" in op_f.new_content
    print("MODIFY (fuzzy) OK")

    # --- DELETE ---
    print("=== 5. parse + apply DELETE ===")
    (tmp / "to_delete.txt").write_text("bye")
    resp_del = json.dumps({
        "file_path": "to_delete.txt",
        "operation": "DELETE",
        "description": "remove temp file",
    })
    op_d = patcher.parse_patch_response(resp_del)
    assert op_d.operation_type == OperationType.DELETE
    ok = patcher.apply_patch(op_d)
    assert ok
    assert not (tmp / "to_delete.txt").exists()
    print("DELETE OK")

    # --- Backup + rollback ---
    print("=== 6. Backup and rollback ===")
    backups = list((tmp / ".localforge" / "backups").iterdir())
    assert len(backups) >= 1, f"Expected backups, found {backups}"
    ts = backups[0].name
    # Overwrite modified file
    (tmp / "src" / "hello.py").write_text("OVERWRITTEN")
    ok = patcher.rollback(ts)
    assert ok
    restored = (tmp / "src" / "hello.py").read_text()
    assert "OVERWRITTEN" not in restored
    print("Rollback OK")

    # --- generate_diff ---
    print("=== 7. generate_diff ===")
    diff = patcher.generate_diff("a=1\n", "a=2\n", "test.py")
    assert "--- a/test.py" in diff
    assert "+++ b/test.py" in diff
    print("generate_diff OK")

    # --- show_diff ---
    print("=== 8. show_diff ===")
    patcher.show_diff(op_m)
    print("show_diff OK")

    # --- find_fuzzy ---
    print("=== 9. find_fuzzy ===")
    s, e = patcher.find_fuzzy("hello world foo", "hello world foo", 0.9)
    assert s == 0 and e == 15
    s, e = patcher.find_fuzzy("hello world foo", "completely different text", 0.9)
    assert s == -1 and e == -1
    print("find_fuzzy OK")

    # --- Validator syntax ---
    print("=== 10. validate_syntax ===")
    ok, err = validator.validate_syntax("a.py", "def f(): pass")
    assert ok and err == ""
    ok, err = validator.validate_syntax("a.py", "def f( pass")
    assert not ok and err != ""
    ok, err = validator.validate_syntax("a.json", '{"a": 1}')
    assert ok
    ok, err = validator.validate_syntax("a.json", "{bad}")
    assert not ok
    ok, err = validator.validate_syntax("a.yaml", "key: value")
    assert ok
    ok, err = validator.validate_syntax("a.txt", "anything")
    assert ok
    print("validate_syntax OK")

    # --- Validator safety ---
    print("=== 11. validate_patch_safety ===")
    safe_op = PatchOperation(
        file_path="a.py", operation_type=OperationType.MODIFY,
        new_content="x = 1", diff="", description="",
    )
    ok, warns = validator.validate_patch_safety(safe_op)
    assert ok and len(warns) == 0

    unsafe_op = PatchOperation(
        file_path="a.py", operation_type=OperationType.MODIFY,
        new_content='import os; os.system("rm -rf /")\neval(input())\npassword="secret123"',
        diff="", description="",
    )
    ok, warns = validator.validate_patch_safety(unsafe_op)
    assert not ok
    assert len(warns) >= 3, f"Expected >=3 warnings, got {warns}"
    print(f"Safety warnings: {warns}")

    del_op = PatchOperation(
        file_path="a.py", operation_type=OperationType.DELETE,
        new_content=None, diff="", description="",
    )
    ok, warns = validator.validate_patch_safety(del_op)
    assert ok
    print("validate_patch_safety OK")

    print()
    print("ALL PATCHING TESTS PASSED")

finally:
    shutil.rmtree(tmp, ignore_errors=True)
