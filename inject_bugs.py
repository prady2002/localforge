"""Inject all 6 bugs directly to disk files, bypassing editor buffer."""
import re

# ── Bug 3: validate_tool_call returns string instead of None ──
f = "localforge/chat/tools.py"
txt = open(f, "r", encoding="utf-8").read()

# Find the specific return None in validate_tool_call
marker = "# \u2500\u2500 Tool call hashing for loop detection"
idx_marker = txt.find(marker)
if idx_marker > 0:
    before = txt[:idx_marker]
    # Find the last "return None" before the marker
    ri = before.rfind("return None")
    if ri > 0:
        # Replace just this return None
        txt = txt[:ri] + 'return f"Tool \'{{}}\' validated".format(tool_call.get("tool", ""))' + txt[ri + len("return None"):]
        print("Bug3: injected validate_tool_call returns string")
    else:
        print("Bug3: SKIP - return None not found before marker")
else:
    print("Bug3: SKIP - marker not found")

# ── Bug 4: hash_tool_call missing sort_keys, shorter hash ──
old4 = 'json.dumps({"tool": tool_name, "args": args}, sort_keys=True, default=str)'
new4 = 'json.dumps({"tool": tool_name, "args": args}, default=str)'
if old4 in txt:
    txt = txt.replace(old4, new4, 1)
    print("Bug4a: removed sort_keys")
else:
    print("Bug4a: SKIP - already removed or not found")

old4b = ".hexdigest()[:12]"
new4b = ".hexdigest()[:8]"
if old4b in txt:
    txt = txt.replace(old4b, new4b, 1)
    print("Bug4b: shortened hash to 8")
else:
    print("Bug4b: SKIP - already shortened or not found")

open(f, "w", encoding="utf-8").write(txt)
print(f"  Wrote {f}")

# ── Bug 6: budget.py ascending sort ──
f2 = "localforge/context_manager/budget.py"
txt2 = open(f2, "r", encoding="utf-8").read()
old6 = "reverse=True"
new6 = "reverse=False"
if old6 in txt2:
    txt2 = txt2.replace(old6, new6, 1)
    print("Bug6: reversed budget sort order")
else:
    print("Bug6: SKIP - already reversed or not found")
open(f2, "w", encoding="utf-8").write(txt2)
print(f"  Wrote {f2}")

# ── Bug 1: _is_debugging_query logic inversion (verify) ──
f3 = "localforge/cloud/engine.py"
txt3 = open(f3, "r", encoding="utf-8").read()
check1 = "if not any(w in lower for w in scaffolding_words):"
if check1 in txt3:
    print("Bug1: already injected (negated scaffolding check)")
else:
    print("Bug1: NOT injected - need to fix")

# ── Bug 2: truncation overlap (verify) ──
check2 = "tail = int(max_chars * 0.5)"
if check2 in txt3:
    print("Bug2: already injected (tail=0.5)")
else:
    print("Bug2: NOT injected - need to fix")

# ── Bug 5: session slicing (verify) ──
f5 = "localforge/chat/session.py"
txt5 = open(f5, "r", encoding="utf-8").read()
check5 = "-(max_messages - 2):-2]"
if check5 in txt5:
    print("Bug5: already injected (drops 2 recent msgs)")
else:
    print("Bug5: NOT injected - need to fix")

print("\n=== All bugs checked ===")
