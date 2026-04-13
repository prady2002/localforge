"""Tests for cloud engine query classification and scaffolding workflow."""

from __future__ import annotations

from localforge.cloud.engine import (
    _classify_query,
    _compress_create_project_result,
    _is_debugging_query,
    _is_large_scaffolding_query,
    _is_scaffolding_query,
    _is_test_fix_query,
    _prune_working_messages,
)


# ---------------------------------------------------------------------------
# Query classification tests
# ---------------------------------------------------------------------------


class TestClassifyQuery:
    """Test _classify_query routes to correct types."""

    # -- Scaffolding queries --

    def test_build_app(self):
        assert _classify_query("build a FastAPI task manager app") in ("scaffolding", "large_scaffolding")

    def test_create_project(self):
        assert _classify_query("create a new React dashboard application") in ("scaffolding", "large_scaffolding")

    def test_scaffold_project(self):
        assert _classify_query("scaffold a Node.js REST API") in ("scaffolding", "large_scaffolding")

    def test_make_app(self):
        assert _classify_query("make a Python CLI tool for file management") in ("scaffolding", "large_scaffolding")

    def test_generate_project(self):
        assert _classify_query("generate a Go microservice with gRPC") in ("scaffolding", "large_scaffolding")

    # -- Large scaffolding queries --

    def test_large_scaffolding_long_prompt(self):
        query = (
            "Build a complete full-stack task management application with the following features: "
            "1) FastAPI backend with SQLite database, user authentication with JWT tokens, "
            "2) CRUD operations for tasks with categories and priorities, "
            "3) Task assignment between users, due dates with reminders, "
            "4) Task comments and attachments support, "
            "5) REST API with proper error handling and validation using Pydantic models, "
            "6) A comprehensive test suite using pytest with at least 15 tests"
        )
        assert _classify_query(query) == "large_scaffolding"

    def test_large_scaffolding_multiple_features(self):
        query = "Build a comprehensive e-commerce platform with authentication, product catalog, cart, checkout, payments, reviews, and admin dashboard"
        assert _classify_query(query) == "large_scaffolding"

    def test_large_scaffolding_fullstack(self):
        query = "Create a full-stack application with React frontend and Express backend, including a database and test suite"
        assert _classify_query(query) == "large_scaffolding"

    # -- Test-fix queries --

    def test_fix_failing_tests(self):
        assert _classify_query("fix the failing tests in test_api.py") == "test_fix"

    def test_make_tests_pass(self):
        assert _classify_query("make all tests pass") == "test_fix"

    def test_run_and_fix_tests(self):
        assert _classify_query("run tests and fix any failures") == "test_fix"

    # -- Debugging queries --

    def test_debug_crash(self):
        assert _classify_query("the app crashes when I submit a form") == "debugging"

    def test_fix_error(self):
        assert _classify_query("there's an error in the login function") == "debugging"

    def test_investigate_bug(self):
        assert _classify_query("investigate why the API returns 500") == "debugging"

    # -- Analysis queries --

    def test_how_to_question(self):
        assert _classify_query("how to use the API?") == "analysis"

    def test_what_is_question(self):
        assert _classify_query("what is the purpose of this module?") == "analysis"

    def test_explain_code(self):
        assert _classify_query("explain the authentication flow") == "analysis"

    def test_show_me(self):
        assert _classify_query("show me the project structure") == "analysis"

    # -- Action queries --

    def test_run_command(self):
        assert _classify_query("run pytest") == "action"

    def test_edit_file(self):
        assert _classify_query("edit the config file to change the port") == "action"

    def test_add_feature(self):
        assert _classify_query("add a new endpoint for user profiles") == "action"

    # -- Edge cases: questions that involve action --

    def test_can_you_fix(self):
        # "can you fix the login bug?" mentions "bug" so routes to debugging, which is correct
        result = _classify_query("can you fix the login bug?")
        assert result in ("debugging", "action")

    def test_can_you_create(self):
        result = _classify_query("can you create a new test file?")
        assert result in ("action", "scaffolding")  # Either is acceptable

    def test_how_to_run_is_analysis(self):
        assert _classify_query("how to run this app?") == "analysis"


# ---------------------------------------------------------------------------
# Scaffolding query detection tests
# ---------------------------------------------------------------------------


class TestIsScaffoldingQuery:
    def test_build_a(self):
        assert _is_scaffolding_query("build a web app")

    def test_create_an(self):
        assert _is_scaffolding_query("create an api service")

    def test_i_want(self):
        assert _is_scaffolding_query("i want a dashboard application")

    def test_i_need(self):
        assert _is_scaffolding_query("i need a rest api backend")

    def test_not_scaffolding(self):
        assert not _is_scaffolding_query("fix the bug in main.py")

    def test_not_scaffolding_question(self):
        assert not _is_scaffolding_query("what is the main function?")


class TestIsLargeScaffoldingQuery:
    def test_long_prompt_with_keywords(self):
        query = "Build a complete enterprise application with authentication, database, " + "x" * 200
        assert _is_large_scaffolding_query(query.lower(), query)

    def test_many_commas(self):
        query = "Build with auth, db, api, tests, docs, frontend, backend"
        assert _is_large_scaffolding_query(query.lower(), query)

    def test_numbered_list(self):
        query = "Build: 1) auth 2) db 3) api 4) tests"
        assert _is_large_scaffolding_query(query.lower(), query)

    def test_small_project(self):
        query = "Build a hello world app"
        assert not _is_large_scaffolding_query(query.lower(), query)


# ---------------------------------------------------------------------------
# Test-fix and debugging detection tests
# ---------------------------------------------------------------------------


class TestIsTestFixQuery:
    def test_fix_tests(self):
        assert _is_test_fix_query("fix the failing tests")

    def test_make_tests_pass(self):
        assert _is_test_fix_query("make tests pass")

    def test_not_test_fix(self):
        assert not _is_test_fix_query("add a new feature")


class TestIsDebuggingQuery:
    def test_bug(self):
        assert _is_debugging_query("there's a bug in the code")

    def test_crash(self):
        assert _is_debugging_query("the app crashes on startup")

    def test_not_debug(self):
        assert not _is_debugging_query("add a new button")

    def test_not_debug_build(self):
        # "build" + error should not be debugging (could be scaffolding)
        assert not _is_debugging_query("build a new error handling system")


# ---------------------------------------------------------------------------
# Context management tests
# ---------------------------------------------------------------------------


class TestCompressCreateProjectResult:
    def test_compresses_large_result(self):
        lines = ["Created project at ../myapp: 15 files written (10,000 bytes total)"]
        for i in range(15):
            lines.append(f"  ✓ file{i}.py (500 bytes)")
        lines.append("\nPROJECT TREE (../myapp):")
        lines.append("  src/")
        lines.append("    main.py")
        lines.append("")
        lines.append("\nNEXT STEPS:")
        lines.append("  1. pip install -r requirements.txt")

        result = "\n".join(lines)
        compressed = _compress_create_project_result(result)

        # Should keep summary, tree, next steps but drop per-file lines
        assert "Created project" in compressed
        assert "PROJECT TREE" in compressed
        assert "NEXT STEPS" in compressed
        assert "✓ file5.py" not in compressed  # Individual file lines should be dropped

    def test_keeps_errors(self):
        result = "Created project at ../myapp: 2 files written, 1 errors\n  ✓ a.py (100 bytes)\n  ✗ b.py: Permission denied"
        compressed = _compress_create_project_result(result)
        assert "✗ b.py" in compressed


class TestPruneWorkingMessages:
    def test_no_pruning_needed(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = _prune_working_messages(msgs, max_chars=1000)
        assert len(result) == 1

    def test_prunes_middle_messages(self):
        msgs = [
            {"role": "user", "content": "a" * 100},
            {"role": "assistant", "content": "b" * 100},
        ]
        for _ in range(20):
            msgs.append({"role": "user", "content": "x" * 5000})
            msgs.append({"role": "assistant", "content": "y" * 5000})

        result = _prune_working_messages(msgs, max_chars=30000)
        assert len(result) < len(msgs)
        # First 2 messages (original request) should be preserved
        assert result[0]["content"] == "a" * 100
        assert result[1]["content"] == "b" * 100

    def test_keeps_all_if_under_budget(self):
        msgs = [{"role": "user", "content": "small"}] * 5
        result = _prune_working_messages(msgs, max_chars=100000)
        assert len(result) == 5
