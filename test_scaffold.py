"""Integration test: scaffold a project from scratch.

Sends a prompt like 'build me a Flask todo app' to an empty directory
and verifies localforge creates a working project.
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

TEST_DIR = Path(os.environ.get(
    "LF_TEST_DIR",
    os.path.join(os.environ.get("TEMP", "/tmp"), "lf_scaffold_test"),
))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from localforge.core.config import LocalForgeConfig, load_config
from localforge.core.ollama_client import OllamaClient
from localforge.chat.engine import ChatEngine


async def main():
    # Clean slate
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR, ignore_errors=True)
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Test directory: {TEST_DIR}")

    config = load_config(str(TEST_DIR))
    config = LocalForgeConfig(**{**config.model_dump(), "repo_path": str(TEST_DIR)})

    ollama = OllamaClient(config)
    engine = ChatEngine(config, ollama, TEST_DIR)

    try:
        try:
            ctx = await ollama.detect_context_window()
            print(f"Context window: {ctx}")
        except Exception as e:
            print(f"Context detection failed: {e}")

        try:
            await ollama.preload_model()
        except Exception:
            pass

        prompt = (
            "Create a Python CLI calculator app in this directory. "
            "It should support add, subtract, multiply, divide via command-line arguments. "
            "Include a main.py and a test_calc.py with pytest tests. "
            "After creating the files, run the tests to make sure they pass."
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

    # ── Post-test verification ──────────────────────────────
    print(f"\n{'='*60}")
    print("POST-TEST VERIFICATION")
    print(f"{'='*60}")

    # 1. Check files were created
    files = list(TEST_DIR.rglob("*.py"))
    lf_files = [f for f in files if ".localforge" not in str(f)]
    print(f"\nPython files created: {len(lf_files)}")
    for f in sorted(lf_files):
        print(f"  {f.relative_to(TEST_DIR)}")

    if not lf_files:
        print("\n❌ FAIL: No Python files were created!")
        sys.exit(1)

    # 2. Check for main.py or similar entry point
    has_main = any("main" in f.name.lower() or "calc" in f.name.lower() for f in lf_files)
    has_test = any("test" in f.name.lower() for f in lf_files)
    print(f"\nHas main/calc file: {'✓' if has_main else '✗'}")
    print(f"Has test file: {'✓' if has_test else '✗'}")

    # 3. Run tests if they exist
    if has_test:
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-v", "."],
            capture_output=True, text=True,
            cwd=str(TEST_DIR),
        )
        print(f"\nTest output:\n{result.stdout[-1500:] if result.stdout else '(no output)'}")
        if result.stderr:
            print(f"STDERR: {result.stderr[-500:]}")
        print(f"Test exit code: {result.returncode}")
        if result.returncode == 0:
            print("\n✅ PASS: All tests pass!")
        else:
            print("\n⚠ Tests did not pass (model may need more iterations)")
    else:
        print("\n⚠ No test file found to verify")

    # Summary
    print(f"\n{'='*60}")
    print("SCAFFOLD TEST SUMMARY")
    print(f"{'='*60}")
    print(f"Files created: {len(lf_files)}")
    print(f"Main file: {'✓' if has_main else '✗'}")
    print(f"Test file: {'✓' if has_test else '✗'}")


if __name__ == "__main__":
    asyncio.run(main())
