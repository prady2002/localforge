"""Integration test: debug and fix a known bug.

Creates a project with a deliberate bug, then asks localforge to find and fix it.
"""
import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

TEST_DIR = Path(os.environ.get(
    "LF_TEST_DIR",
    os.path.join(os.environ.get("TEMP", "/tmp"), "lf_debug_test"),
))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from localforge.chat.engine import ChatEngine
from localforge.core.config import LocalForgeConfig, load_config
from localforge.core.ollama_client import OllamaClient

# ── Buggy project files ──────────────────────────────────

MAIN_PY = '''\
"""Simple shopping cart module."""


def calculate_total(items):
    """Calculate total price of items with tax.

    Each item is a dict with 'name', 'price', 'quantity'.
    Tax rate is 13%.
    """
    total = 0
    for item in items:
        total += item["price"] * item["quantity"]
    # BUG: tax is subtracted instead of added
    total = total - (total * 0.13)
    return round(total, 2)


def apply_discount(total, discount_percent):
    """Apply a percentage discount to the total."""
    if discount_percent < 0 or discount_percent > 100:
        raise ValueError("Discount must be between 0 and 100")
    # BUG: discount is added instead of subtracted
    return round(total + (total * discount_percent / 100), 2)


def format_receipt(items, discount_percent=0):
    """Format a receipt string."""
    total = calculate_total(items)
    if discount_percent > 0:
        total = apply_discount(total, discount_percent)
    return f"Total: ${total:.2f}"
'''

TEST_PY = '''\
"""Tests for shopping cart."""
import pytest
from cart import calculate_total, apply_discount, format_receipt


def test_calculate_total_simple():
    items = [{"name": "Widget", "price": 10.00, "quantity": 2}]
    # Expected: 20.00 * 1.13 = 22.60
    assert calculate_total(items) == 22.60


def test_calculate_total_multiple():
    items = [
        {"name": "Widget", "price": 10.00, "quantity": 2},
        {"name": "Gadget", "price": 5.00, "quantity": 3},
    ]
    # Expected: (20 + 15) * 1.13 = 39.55
    assert calculate_total(items) == 39.55


def test_apply_discount():
    # 10% off $100 = $90
    assert apply_discount(100.0, 10) == 90.0


def test_apply_discount_zero():
    assert apply_discount(50.0, 0) == 50.0


def test_apply_discount_invalid():
    with pytest.raises(ValueError):
        apply_discount(100.0, -5)
    with pytest.raises(ValueError):
        apply_discount(100.0, 150)


def test_format_receipt():
    items = [{"name": "Widget", "price": 10.00, "quantity": 1}]
    receipt = format_receipt(items)
    assert "11.30" in receipt  # 10 * 1.13 = 11.30
'''


async def main():
    # Clean slate
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR, ignore_errors=True)
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Test directory: {TEST_DIR}")

    # Write the buggy files
    (TEST_DIR / "cart.py").write_text(MAIN_PY, encoding="utf-8")
    (TEST_DIR / "test_cart.py").write_text(TEST_PY, encoding="utf-8")

    # Show the bugs exist
    print("\nPRE-TEST: Running tests to show failures...")
    pre = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "."],
        capture_output=True, text=True, cwd=str(TEST_DIR),
    )
    print(pre.stdout[-800:] if pre.stdout else "(no output)")
    print(f"Pre-test exit code: {pre.returncode}")
    failed_count = pre.stdout.count("FAILED")
    print(f"Failed tests: {failed_count}")

    # Now let localforge fix it
    config = load_config(str(TEST_DIR))
    config = LocalForgeConfig(**{**config.model_dump(), "repo_path": str(TEST_DIR)})
    ollama = OllamaClient(config)
    engine = ChatEngine(config, ollama, TEST_DIR)

    try:
        with contextlib.suppress(Exception):
            await ollama.detect_context_window()
        with contextlib.suppress(Exception):
            await ollama.preload_model()

        prompt = (
            "The tests in test_cart.py are failing. "
            "Run the tests, find the bugs in cart.py, fix them, "
            "and make all tests pass."
        )
        print(f"\n{'='*60}")
        print(f"PROMPT: {prompt}")
        print(f"{'='*60}\n")

        response = await engine.send_message(prompt)

        print(f"\n{'='*60}")
        print("FINAL RESPONSE:")
        print(f"{'='*60}")
        print(response[:2000])

    finally:
        await ollama.close()

    # Post-test verification
    print(f"\n{'='*60}")
    print("POST-TEST VERIFICATION")
    print(f"{'='*60}")
    post = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "."],
        capture_output=True, text=True, cwd=str(TEST_DIR),
    )
    print(post.stdout[-1000:] if post.stdout else "(no output)")
    print(f"Post-test exit code: {post.returncode}")

    if post.returncode == 0:
        print("\n PASS: All tests pass after debugging!")
    else:
        passed = post.stdout.count("PASSED")
        failed = post.stdout.count("FAILED")
        print(f"\n Tests: {passed} passed, {failed} failed")

    # Check what was changed
    print(f"\n{'='*60}")
    print("FIXED cart.py:")
    print(f"{'='*60}")
    fixed = (TEST_DIR / "cart.py").read_text(encoding="utf-8")
    print(fixed)


if __name__ == "__main__":
    asyncio.run(main())
