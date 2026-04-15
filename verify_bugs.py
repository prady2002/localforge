"""Quick verification that all 6 bugs are active in the codebase."""
from localforge.chat.tools import validate_tool_call, hash_tool_call
from localforge.cloud.engine import _truncate_tool_result
from localforge.context_manager.budget import TokenBudgetManager
from localforge.core.config import LocalForgeConfig
from localforge.core.models import FileChunk
import re

# Bug 3: validate_tool_call should return None but returns string
tc = {"tool": "read_file", "args": {"path": "test.py"}}
r = validate_tool_call(tc)
print(f"Bug3 validate_tool_call: {r!r}  (is None: {r is None})")

# Bug 4: hash without sort_keys
h1 = hash_tool_call("edit", {"b": 1, "a": 2})
h2 = hash_tool_call("edit", {"a": 2, "b": 1})
print(f"Bug4 hash: h1={h1} h2={h2} same={h1==h2} len={len(h1)}")

# Bug 2: truncation overlap (0.6 + 0.5 = 1.1 > 1.0)
t = _truncate_tool_result("x" * 200, max_chars=100)
m = re.search(r"\((-?\d+) characters", t)
omitted = int(m.group(1)) if m else None
print(f"Bug2 truncate omitted={omitted} (should be positive, may be negative with bug)")

# Bug 6: budget ascending sort
mgr = TokenBudgetManager(LocalForgeConfig())
chunks = [
    FileChunk(file_path="low.py", start_line=1, end_line=5, content="low", score=0.1),
    FileChunk(file_path="high.py", start_line=1, end_line=5, content="high", score=0.9),
]
sel = mgr.fit_chunks_to_budget(chunks, budget=1000)
print(f"Bug6 budget first: {sel[0].file_path} (score={sel[0].score})")

# Bug 1: debugging query
from localforge.cloud.engine import _is_debugging_query
print(f"Bug1 _is_debugging_query('there is a bug'): {_is_debugging_query('there is a bug')}")

# Bug 5: session slicing
from localforge.chat.session import ChatSession
s = ChatSession(session_id="test", repo_path=".")
for i in range(10):
    s.add_user_message(f"msg_{i}")
msgs = s.get_ollama_messages(max_messages=5)
contents = [m["content"] for m in msgs]
print(f"Bug5 session msgs: {contents} (msg_9 present: {'msg_9' in contents})")
