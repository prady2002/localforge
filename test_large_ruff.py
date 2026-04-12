"""Integration test: ruff fix on a project with MANY issues.

Tests that localforge handles large tool output without connection errors.
Previously failed because ruff output for 75+ issues overwhelmed the context.
"""
import asyncio
import contextlib
import os
import shutil
import sys
from pathlib import Path

TEST_DIR = Path(os.environ.get(
    "LF_TEST_DIR",
    os.path.join(os.environ.get("TEMP", "/tmp"), "lf_large_ruff_test"),
))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from localforge.core.config import LocalForgeConfig, load_config
from localforge.core.ollama_client import OllamaClient
from localforge.chat.engine import ChatEngine


# Generate a file with MANY ruff issues (tabs, long lines, unused imports, etc.)
BUGGY_CODE = '''\
import os
import sys
import json
import re
import math
import random
import collections
import itertools
import functools
import pathlib

l = [1, 2, 3]
O = "hello"
I = 42

def calculate_stuff(x,y,z):
\tresult = x+y+z
\tif result > 100:
\t\treturn True
\telse:
\t\treturn False

def process_data(data):
\tl = len(data)
\tfor i in range(l):
\t\titem = data[i]
\t\tif item == None:
\t\t\tcontinue
\t\tprint(item)

def long_function_name_that_makes_lines_very_very_long(argument_one, argument_two, argument_three, argument_four, argument_five):
\treturn argument_one + argument_two + argument_three + argument_four + argument_five

class MyClass:
\tdef __init__(self):
\t\tself.x = 1
\t\tself.y = 2

\tdef method_one(self):
\t\tl = [1,2,3,4,5]
\t\tresult = list(map(lambda x: x*2, l))
\t\treturn result

\tdef method_two(self):
\t\tdict = {"key": "value"}
\t\tlist = [1, 2, 3]
\t\treturn dict, list

\tdef method_three(self):
\t\ttry:
\t\t\tx = 1/0
\t\texcept:
\t\t\tpass

def another_function():
\tl = 10
\tresult = l * 2
\tif result == True:
\t\tprint("yes")
\telif result == False:
\t\tprint("no")

def unused_args(a, b, c, d, e):
\treturn a + b

x = 1; y = 2; z = 3

def bare_except_function():
\ttry:
\t\topen("nonexistent.txt")
\texcept:
\t\tprint("error")

def very_long_variable_names():
\tthis_is_a_very_long_variable_name_that_exceeds_the_line_length_limit_of_one_hundred_characters = 42
\tanother_extremely_long_variable_name_that_also_exceeds_line_limits_and_should_be_flagged = 99
\tresult = this_is_a_very_long_variable_name_that_exceeds_the_line_length_limit_of_one_hundred_characters + another_extremely_long_variable_name_that_also_exceeds_line_limits_and_should_be_flagged
\treturn result
'''

BUGGY_CODE_2 = '''\
import os, sys
from os.path import *

l = 5
O = 10

def foo(x):
\tif x > 0:
\t\tif x < 10:
\t\t\treturn True
\t\telse:
\t\t\treturn False
\telse:
\t\treturn None

class BadStyle:
\tdef __init__(self,x,y):
\t\tself.x=x
\t\tself.y=y

\tdef method(self):
\t\tl=[1,2,3]
\t\tfor i in range(len(l)):
\t\t\tprint(l[i])
'''


async def main():
    # Clean slate
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR, ignore_errors=True)
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Test directory: {TEST_DIR}")

    # Write buggy files
    (TEST_DIR / "calculator.py").write_text(BUGGY_CODE, encoding="utf-8")
    (TEST_DIR / "helper.py").write_text(BUGGY_CODE_2, encoding="utf-8")

    # Add pyproject.toml for ruff config
    (TEST_DIR / "pyproject.toml").write_text(
        '[tool.ruff]\nline-length = 100\ntarget-version = "py311"\n\n'
        '[tool.ruff.lint]\nselect = ["E", "F", "W", "N", "UP", "B", "SIM"]\n',
        encoding="utf-8",
    )

    # Show how many issues exist
    import subprocess
    pre = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "."],
        capture_output=True, text=True, cwd=str(TEST_DIR),
    )
    issue_count = pre.stdout.strip().count("\n") + 1 if pre.stdout.strip() else 0
    print(f"\nPRE-TEST: {issue_count} ruff issues found")
    print(f"Output size: {len(pre.stdout)} bytes")

    config = load_config(str(TEST_DIR))
    config = LocalForgeConfig(**{**config.model_dump(), "repo_path": str(TEST_DIR)})
    ollama = OllamaClient(config)
    engine = ChatEngine(config, ollama, TEST_DIR)

    try:
        with contextlib.suppress(Exception):
            await ollama.detect_context_window()
        with contextlib.suppress(Exception):
            await ollama.preload_model()

        prompt = "run ruff check . and fix all the issues it finds"
        print(f"\n{'='*60}")
        print(f"PROMPT: {prompt}")
        print(f"{'='*60}\n")

        response = await engine.send_message(prompt)

        print(f"\n{'='*60}")
        print("FINAL RESPONSE:")
        print(f"{'='*60}")
        print(response[:2000])

        # Check for connection error
        if "connection error" in response.lower():
            print("\n❌ FAIL: Connection error occurred!")
            sys.exit(1)

    finally:
        await ollama.close()

    # Post-test verification
    print(f"\n{'='*60}")
    print("POST-TEST VERIFICATION")
    print(f"{'='*60}")
    post = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "."],
        capture_output=True, text=True, cwd=str(TEST_DIR),
    )
    post_count = post.stdout.strip().count("\n") + 1 if post.stdout.strip() else 0
    print(f"Remaining issues: {post_count}")
    print(f"Exit code: {post.returncode}")

    if post.returncode == 0:
        print("\n✅ PASS: All ruff issues fixed!")
    else:
        fixed = issue_count - post_count
        print(f"\nFixed {fixed}/{issue_count} issues ({100*fixed//max(issue_count,1)}%)")
        if fixed > 0:
            print("⚠ Partial fix (model may need more iterations)")
        else:
            print("❌ FAIL: No issues fixed")


if __name__ == "__main__":
    asyncio.run(main())
