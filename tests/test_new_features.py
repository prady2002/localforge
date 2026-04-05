"""Tests for the interactive chat module."""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# ChatSession
# ---------------------------------------------------------------------------


class TestChatSession:
    def test_add_messages(self):
        from localforge.chat.session import ChatSession

        session = ChatSession(session_id="test", repo_path=".")
        session.add_user_message("hello")
        session.add_assistant_message("hi there")

        assert len(session.messages) == 2
        assert session.messages[0].role == "user"
        assert session.messages[0].content == "hello"
        assert session.messages[1].role == "assistant"

    def test_get_ollama_messages(self):
        from localforge.chat.session import ChatSession

        session = ChatSession(session_id="test", repo_path=".")
        session.add_user_message("hello")
        session.add_assistant_message("hi there")

        msgs = session.get_ollama_messages()
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hello"}
        assert msgs[1] == {"role": "assistant", "content": "hi there"}

    def test_get_ollama_messages_truncation(self):
        from localforge.chat.session import ChatSession

        session = ChatSession(session_id="test", repo_path=".")
        for i in range(10):
            session.add_user_message(f"msg {i}")

        msgs = session.get_ollama_messages(max_messages=3)
        assert len(msgs) == 3

    def test_save_load(self, tmp_path: Path):
        from localforge.chat.session import ChatSession

        session = ChatSession(session_id="test", repo_path=".", model="test-model")
        session.add_user_message("hello")
        session.add_assistant_message("hi!")

        save_path = tmp_path / "session.json"
        session.save(save_path)

        loaded = ChatSession.load(save_path)
        assert loaded.session_id == "test"
        assert loaded.model == "test-model"
        assert len(loaded.messages) == 2

    def test_clear(self):
        from localforge.chat.session import ChatSession

        session = ChatSession(session_id="test", repo_path=".")
        session.add_user_message("hello")
        session.clear()
        assert len(session.messages) == 0


# ---------------------------------------------------------------------------
# Git utils
# ---------------------------------------------------------------------------


class TestGitUtils:
    def test_is_git_repo_false_for_tmp(self, tmp_path: Path):
        from localforge.core.git_utils import is_git_repo

        assert is_git_repo(tmp_path) is False

    def test_get_changed_files_empty_for_non_repo(self, tmp_path: Path):
        from localforge.core.git_utils import get_changed_files

        assert get_changed_files(tmp_path) == []

    def test_get_current_branch_empty_for_non_repo(self, tmp_path: Path):
        from localforge.core.git_utils import get_current_branch

        assert get_current_branch(tmp_path) == ""

    def test_create_checkpoint_returns_none_for_non_repo(self, tmp_path: Path):
        from localforge.core.git_utils import create_checkpoint

        assert create_checkpoint(tmp_path) is None


# ---------------------------------------------------------------------------
# OllamaClient enhancements
# ---------------------------------------------------------------------------


class TestOllamaClientEnhancements:
    def test_get_model_context_window_defaults(self):
        from localforge.core.ollama_client import get_model_context_window

        assert get_model_context_window("llama2:7b") == 4096
        assert get_model_context_window("codellama:13b") >= 8192
        assert get_model_context_window("qwen2.5-coder:7b") == 8192  # 4096 * 2 for coder
        assert get_model_context_window("llama3:70b") == 32768

    def test_stream_to_console_attribute(self):
        from localforge.core.config import LocalForgeConfig
        from localforge.core.ollama_client import OllamaClient

        config = LocalForgeConfig()
        client = OllamaClient(config)
        assert hasattr(client, "stream_to_console")
        assert client.stream_to_console is True


# ---------------------------------------------------------------------------
# StateManager enhancements
# ---------------------------------------------------------------------------


class TestStateManagerEnhancements:
    def test_list_states_empty(self, tmp_path: Path):
        from localforge.agent.state_manager import StateManager

        mgr = StateManager(str(tmp_path / "states"))
        assert mgr.list_states() == []

    def test_list_states_with_data(self, tmp_path: Path):
        from localforge.agent.state_manager import StateManager
        from localforge.core.models import MultiAgentState

        mgr = StateManager(str(tmp_path / "states"))
        state = MultiAgentState(task="test task", iteration=5)
        path = mgr.get_state_path("test task")
        # Override base_dir for this test
        mgr.base_dir = tmp_path / "states"
        actual_path = mgr.base_dir / path.name
        mgr.save_state(state, actual_path)

        states = mgr.list_states()
        assert len(states) == 1
        assert states[0]["task"] == "test task"
        assert states[0]["iteration"] == 5


# ---------------------------------------------------------------------------
# Symbol extraction enhancements
# ---------------------------------------------------------------------------


class TestSymbolExtraction:
    """Test enhanced multi-language symbol extraction."""

    def _extract_symbols(self, content: str, language: str):
        """Helper: run symbol extraction on content and return results."""
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE symbols (file_id INTEGER, name TEXT, kind TEXT, line INTEGER, scope TEXT)"
        )

        from localforge.index.indexer import RepositoryIndexer

        RepositoryIndexer._extract_symbols(1, content, language, conn)
        rows = conn.execute("SELECT name, kind, line, scope FROM symbols").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def test_python_functions_and_classes(self):
        code = "def hello():\n    pass\n\nclass Foo:\n    def bar(self):\n        pass\n"
        symbols = self._extract_symbols(code, "python")
        names = {s["name"] for s in symbols}
        assert "hello" in names
        assert "Foo" in names
        assert "bar" in names

    def test_python_async_def(self):
        code = "async def fetch_data():\n    pass\n"
        symbols = self._extract_symbols(code, "python")
        assert any(s["name"] == "fetch_data" for s in symbols)

    def test_python_constants(self):
        code = "MAX_SIZE = 100\nDEFAULT_NAME = 'foo'\n"
        symbols = self._extract_symbols(code, "python")
        names = {s["name"] for s in symbols}
        assert "MAX_SIZE" in names
        assert "DEFAULT_NAME" in names

    def test_javascript_functions(self):
        code = "function greet() {}\nasync function fetchData() {}\n"
        symbols = self._extract_symbols(code, "javascript")
        names = {s["name"] for s in symbols}
        assert "greet" in names
        assert "fetchData" in names

    def test_javascript_const_exports(self):
        code = "export const API_URL = 'http://example.com'\nconst helper = () => {}\n"
        symbols = self._extract_symbols(code, "javascript")
        names = {s["name"] for s in symbols}
        assert "API_URL" in names
        assert "helper" in names

    def test_typescript_interface_type_enum(self):
        code = "interface User {}\nexport type Config = {}\nenum Color { Red, Blue }\n"
        symbols = self._extract_symbols(code, "typescript")
        names = {s["name"] for s in symbols}
        assert "User" in names
        assert "Config" in names
        assert "Color" in names

    def test_go_func_and_type(self):
        code = "func main() {}\nfunc (s *Server) Start() {}\ntype Server struct {}\n"
        symbols = self._extract_symbols(code, "go")
        names = {s["name"] for s in symbols}
        assert "main" in names
        assert "Start" in names
        assert "Server" in names

    def test_rust_fn_struct_trait(self):
        code = (
            "pub fn run() {}\nfn helper() {}\n"
            "struct Config {}\npub trait Handler {}\n"
            "impl Config {}\n"
        )
        symbols = self._extract_symbols(code, "rust")
        names = {s["name"] for s in symbols}
        assert "run" in names
        assert "helper" in names
        assert "Config" in names
        assert "Handler" in names

    def test_java_class_method(self):
        code = "public class UserService {\n    public void getUser(int id) {}\n}\n"
        symbols = self._extract_symbols(code, "java")
        names = {s["name"] for s in symbols}
        assert "UserService" in names

    def test_ruby_class_and_def(self):
        code = "class MyApp\n  def run\n  end\nend\nmodule Utils\nend\n"
        symbols = self._extract_symbols(code, "ruby")
        names = {s["name"] for s in symbols}
        assert "MyApp" in names
        assert "run" in names
        assert "Utils" in names
