"""Quick integration test: sends a single prompt to ChatEngine and prints the result."""
import asyncio
import sys
import os
from pathlib import Path

# Point to the test project
TEST_DIR = Path(os.environ.get("LF_TEST_DIR", r"C:\Users\prgi10298\AppData\Local\Temp\lf_test_project"))

# Ensure localforge is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from localforge.core.config import LocalForgeConfig, load_config
from localforge.core.ollama_client import OllamaClient
from localforge.chat.engine import ChatEngine


async def main():
    # Load config for the test project directory
    config = load_config(str(TEST_DIR))
    config = LocalForgeConfig(**{**config.model_dump(), "repo_path": str(TEST_DIR)})

    # Use 14b model if available for better results
    # config = config.model_copy(update={"model_name": "qwen2.5-coder:14b"})

    ollama = OllamaClient(config)
    engine = ChatEngine(config, ollama, TEST_DIR)

    try:
        # Detect context window
        try:
            ctx = await ollama.detect_context_window()
            print(f"Context window: {ctx}")
        except Exception as e:
            print(f"Context detection failed: {e}")

        # Preload model
        try:
            await ollama.preload_model()
        except Exception:
            pass

        # Send the test prompt
        prompt = "run ruff check . and fix all the issues it finds"
        print(f"\n{'='*60}")
        print(f"PROMPT: {prompt}")
        print(f"{'='*60}\n")

        response = await engine.send_message(prompt)

        print(f"\n{'='*60}")
        print(f"FINAL RESPONSE:")
        print(f"{'='*60}")
        print(response[:2000])

    finally:
        await ollama.close()

    # Verify: run ruff check again to see remaining issues
    print(f"\n{'='*60}")
    print("POST-TEST VERIFICATION: ruff check .")
    print(f"{'='*60}")
    import subprocess
    result = subprocess.run(
        "python -m ruff check .",
        shell=True, capture_output=True, text=True,
        cwd=str(TEST_DIR),
    )
    print(result.stdout or "(no output)")
    if result.stderr:
        print(f"STDERR: {result.stderr}")
    print(f"Exit code: {result.returncode}")


if __name__ == "__main__":
    asyncio.run(main())
