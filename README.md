# LocalForge

**A local-first, repo-aware AI coding agent powered by Ollama.**

LocalForge is a fully offline, privacy-first AI coding agent that lives in your
terminal. Point it at a codebase, describe a task in plain English, and it will
analyze the code, build an execution plan, generate patches, run verification,
and iterate — all using a local LLM through [Ollama](https://ollama.com).

> **Your code never leaves your machine.** No API keys. No cloud. No telemetry.

---

## Why LocalForge?

| Feature | LocalForge | Cloud-based agents |
|---------|------------|--------------------|
| **Privacy** | 100 % local — code never leaves your machine | Code sent to external servers |
| **Cost** | Free forever (runs on your hardware) | Per-token billing |
| **Internet** | Works fully offline | Requires internet connection |
| **Repo awareness** | SQLite-indexed codebase with FTS5 search | Context window stuffing |
| **Safety** | Diff preview + confirmation + backups + rollback | Varies |
| **Transparency** | Open source, inspectable agent prompts | Black box |

### What makes it stand out

- **Tool-use from chat.** The LLM can autonomously read files, write code,
  edit files, run shell commands, and search the codebase — all from within
  the interactive chat. No more copy-pasting suggestions; the agent acts
  directly on your code, similar to Claude Code.
- **Multi-agent architecture.** Six specialist agents (Analyzer → Planner →
  Coder → Verifier → Reflector → Summarizer) collaborate through structured
  JSON handoffs, each with its own system prompt and output schema. This
  separation of concerns produces more reliable results than single-prompt
  approaches.
- **Interactive chat.** `localforge chat` gives you a conversational REPL —
  ask questions about your codebase, explore code, and plan changes
  interactively. Chat history persists between sessions. Built-in slash
  commands: `/run`, `/read`, `/context`, `/tokens`, and more.
- **Streaming output.** See model responses as they generate, token by token.
  No more staring at a spinner — watch the agent think in real time.
- **Git integration.** Automatic git checkpoints before and after autofix runs.
  Track changes with your existing git workflow alongside file-based backups.
- **Auto-detect context window.** Queries the Ollama API to determine your
  model's actual context window size — no manual configuration needed.
- **Token-budget-aware.** A dedicated context assembler and token budget manager
  keep every prompt within the model's context window — no silent truncation
  surprises.
- **Smart retrieval.** Combines FTS5 full-text search, filename fuzzy matching,
  symbol search, and optional ripgrep integration. Chunks are ranked by
  term-frequency, path relevance, recency, and deduplicated automatically.
- **Multi-language indexing.** Enhanced symbol extraction for Python, JavaScript,
  TypeScript, Go, Rust, Java, C#, Ruby, PHP, C/C++, and more — including
  interfaces, types, enums, constants, async functions, and module exports.
- **Safe by default.** Every patch is shown as a diff, backed up before
  application, validated for syntax and safety, and only written after explicit
  approval (or `--yes`). Rollback is always one command away. Destructive
  shell commands are blocked for safety.
- **Project rules.** Add conventions to `.localforge/rules.md` and they're
  injected into every agent prompt — the agent follows your coding standards.

---

## Table of Contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Quick Start](#quick-start)
4. [Complete CLI Reference](#complete-cli-reference)
5. [Architecture](#architecture)
6. [Agent Loop](#agent-loop)
7. [How Retrieval Works](#how-retrieval-works)
8. [Project Rules](#project-rules)
9. [Configuration Reference](#configuration-reference)
10. [Model Recommendations](#model-recommendations)
11. [Choosing and Switching Models](#choosing-and-switching-models)
12. [Workflow Examples](#workflow-examples)
13. [Safety & Backups](#safety--backups)
14. [Tips for Best Results](#tips-for-best-results)
15. [Limitations](#limitations)
16. [Comparison with Cloud Agents](#comparison-with-cloud-agents)
17. [Changelog](#changelog)
18. [Contributing](#contributing)
19. [License](#license)

---

## Requirements

| Dependency | Version | Notes |
|------------|---------|-------|
| Python | **3.11 +** | 3.12 and 3.13 work too |
| [Ollama](https://ollama.com) | latest | Must be running (`ollama serve`) |
| ripgrep (`rg`) | *optional* | Speeds up file discovery if installed |
| Git | *optional* | Used for change tracking |

### Hardware recommendations

| Setup | VRAM | Recommended model | Profile |
|-------|------|-------------------|---------|
| Minimum | 4–6 GB | `qwen2.5-coder:7b` | `small` |
| Recommended | 8–16 GB | `qwen2.5-coder:14b` | `medium` |
| Best results | 24+ GB | `qwen2.5-coder:32b` | `large` |

---

## Installation

### Recommended: pipx (zero-friction, works immediately)

[`pipx`](https://pipx.pypa.io) installs CLI tools in isolated environments and
automatically wires them into your PATH — no manual setup, works on the first
try on any machine.

```bash
# Install pipx (once per machine)
py -m pip install --user pipx
py -m pipx ensurepath
```

Close and reopen your terminal, then:

```bash
pipx install localforge
localforge --version   # works immediately
```

> **Windows users:** `pipx ensurepath` handles PATH for you. After running it
> once, every subsequent `pipx install <tool>` just works.

### Alternative: plain pip

```bash
pip install localforge
```

> **Note:** On Windows, `pip install` may place the `localforge` command in a
> Scripts directory that is not yet on your PATH. If `localforge` is not
> recognised after install, run the one-time fix below and restart your
> terminal.

#### Windows PATH fix (one time only)

```powershell
py -m localforge setup-shell
```

Then **close and reopen your terminal** — `localforge` will work from that
point on, permanently.

### From source (development)

```bash
git clone https://github.com/localforge/localforge.git
cd localforge
pip install -e ".[dev]"
```

---

## Quick Start

### 1. Install Ollama and pull a model

```bash
# Install Ollama from https://ollama.com
ollama pull qwen2.5-coder:7b
```

### 2. Initialize your project

```bash
cd your-project/
localforge init
```

This creates a `.localforge/` directory with:
- `config.yml` — model and behavior settings
- `rules.md` — project-specific coding rules (injected into every prompt)
- `commands.yml` — custom verification commands

### 3. Index the codebase

```bash
localforge index
```

Builds a SQLite index with full-text search, symbol extraction, and file
metadata. Runs in seconds for most repos. Re-run after major changes.

### 4. Search your code (optional)

```bash
localforge search "authentication"
localforge search "UserModel" --mode symbol
localforge search "config.py" --mode filename
```

### 5. Run the full autofix pipeline

```bash
localforge autofix "fix the authentication bug in the login endpoint"
```

The agent will: analyze → plan → patch → verify → reflect → iterate. All
locally, all with your approval at each step.

---

## Complete CLI Reference

### Global Options

```bash
localforge --version          # Show version
localforge --verbose          # Enable debug logging (aliases: --debug)
localforge --help             # Show all commands
```

### `localforge init`

Initialize a `.localforge/` directory with default configuration files.

```bash
localforge init                 # current directory
localforge init /path/to/repo   # specific repo
```

Creates: `config.yml`, `rules.md`, `commands.yml`.

### `localforge index`

Build or refresh the SQLite code index for fast retrieval. Does **not** require
Ollama to be running.

```bash
localforge index                # incremental update (only re-indexes changed files)
localforge index --force        # full re-index from scratch
localforge index --repo ./myapp
```

| Flag | Description |
|------|-------------|
| `--force` | Re-index all files from scratch |
| `--repo`, `-r` | Path to the repository root (default: `.`) |

### `localforge search`

Search the codebase index directly — great for exploring what the agent will see.

```bash
localforge search "database connection"          # search everything
localforge search "UserModel" --mode symbol       # search symbol names only
localforge search "config" --mode filename        # search file names only
localforge search "authenticate" --mode text      # full-text search only
localforge search "login" --limit 20              # more results
```

| Flag | Description |
|------|-------------|
| `--mode`, `-m` | Search mode: `all`, `text`, `filename`, `symbol` |
| `--limit`, `-n` | Max results (default: `10`) |
| `--repo`, `-r` | Path to the repository root |

### `localforge analyze`

Retrieve the most relevant code chunks for a given task description. Useful to
preview what context the agent will work with.

```bash
localforge analyze "why is the login endpoint slow?"
localforge analyze "add pagination to the users API" --limit 20
localforge analyze "..." --model codellama:13b
```

| Flag | Description |
|------|-----------|
| `--limit`, `-n` | Max chunks to retrieve (default: `10`) |
| `--model`, `-m` | Override the Ollama model |
| `--repo`, `-r` | Path to the repository root |

### `localforge plan`

Run analysis and produce an execution plan, saved to `.localforge/last_plan.json`.

```bash
localforge plan "add input validation to the signup form"
localforge plan "..." --model qwen2.5-coder:32b
```

| Flag | Description |
|------|-----------|
| `--model`, `-m` | Override the Ollama model |
| `--repo`, `-r` | Path to the repository root |

### `localforge patch`

Generate and apply code patches from a saved plan.

```bash
localforge patch "add input validation to the signup form"
localforge patch "fix bug" --step 2          # execute only step 2
localforge patch "fix bug" --dry-run         # preview without writing
localforge patch "fix bug" --yes             # auto-approve all patches
localforge patch "fix bug" --model codellama:13b
```

| Flag | Description |
|------|-----------|
| `--step`, `-s` | Execute only this step number |
| `--dry-run` | Show patches without applying |
| `--yes`, `-y` | Auto-approve all patches |
| `--model`, `-m` | Override the Ollama model |
| `--repo`, `-r` | Path to the repository root |

### `localforge verify`

Run the project's verification suite. Auto-detects: **pytest**, **ruff**,
**mypy**, **npm test**, **go test**.

```bash
localforge verify
localforge verify --repo ./myapp
```

### `localforge autofix`

**The main command.** Runs the full agent pipeline end-to-end: analyze → plan →
patch → verify → reflect → iterate.

```bash
localforge autofix "fix the failing test in test_users.py"
localforge autofix "refactor the database layer to use async" --model codellama:13b
localforge autofix "add caching to the API" --yes --profile large
```

| Flag | Description |
|------|-------------|
| `--yes`, `-y` | Auto-approve all patches |
| `--dry-run` | Show patches without applying them |
| `--model`, `-m` | Override the Ollama model |
| `--profile`, `-p` | Model profile: `small`, `medium`, `large` |
| `--max-iterations` | Override max agent iterations |
| `--repo`, `-r` | Path to the repository root |

### `localforge diff`

Show unified diffs for changes made by localforge (uses the backup system).

```bash
localforge diff                       # latest backup
localforge diff 20260403_143022       # specific timestamp
```

### `localforge rollback`

Undo changes by restoring files from a backup snapshot.

```bash
localforge rollback                   # list available backups
localforge rollback 20260403T143022   # restore specific backup
```

### `localforge status`

Show project status: index stats, Ollama health, model info, git status, and last task.

```bash
localforge status
```

### `localforge models`

List all available models on your Ollama instance and show the current default.

```bash
localforge models
```

### `localforge set-model`

Set your default model interactively or by specifying a model name directly.

```bash
localforge set-model                    # interactive selection
localforge set-model qwen2.5-coder:14b  # set directly
```

| Flag | Description |
|------|-------------|
| `--repo`, `-r` | Path to the repository root |

### `localforge chat`

Start an interactive conversational REPL about your codebase. Ask questions,
explore code, and plan changes interactively. Chat history persists between sessions.

```bash
localforge chat
localforge chat --model codellama:13b
```

| Flag | Description |
|------|-------------|
| `--model`, `-m` | Override the Ollama model |
| `--repo`, `-r` | Path to the repository root |

**In-chat commands:**

| Command | Description |
|---------|-------------|
| `/clear` | Clear chat history |
| `/context <query>` | Search codebase and show matching context |
| `/history` | Show conversation history |
| `/help` | Show available commands |
| `/quit` | Exit the chat |

### `localforge history`

Show a list of previous autofix task runs.

```bash
localforge history
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                       CLI (Typer)                           │
│ init │ index │ search │ analyze │ plan │ patch │ verify     │
│ autofix │ diff │ rollback │ status │ chat │ history         │
└───────────────────────┬─────────────────────────────────────┘
                        │
           ┌────────────▼────────────┐
           │   AgentOrchestrator     │
           │  (coordinates pipeline) │
           └────┬───┬───┬───┬───┬───┘
                │   │   │   │   │
     ┌──────────┘   │   │   │   └──────────┐
     ▼              ▼   ▼   ▼              ▼
┌─────────┐  ┌────────┐ ┌────────┐  ┌───────────┐
│ Analyzer│  │Planner │ │ Coder  │  │ Verifier  │
└─────────┘  └────────┘ └────────┘  └───────────┘
                            │              │
                     ┌──────┘     ┌────────┘
                     ▼            ▼
               ┌──────────┐ ┌──────────┐
               │Reflector │ │Summarizer│
               └──────────┘ └──────────┘

     ┌─────────────────────────────────────────────┐
     │              Support Layer                   │
     │                                              │
     │  ┌──────────────┐  ┌───────────────────┐    │
     │  │  Repository   │  │  Context Manager  │    │
     │  │   Indexer     │  │  (Budget + Asm.)  │    │
     │  │  (SQLite)     │  │                   │    │
     │  └──────────────┘  └───────────────────┘    │
     │                                              │
     │  ┌──────────────┐  ┌───────────────────┐    │
     │  │  Retriever   │  │   File Patcher    │    │
     │  │  + Ranking   │  │  (backup + apply) │    │
     │  └──────────────┘  └───────────────────┘    │
     │                                              │
     │  ┌──────────────┐  ┌───────────────────┐    │
     │  │ Ollama Client│  │ Verification      │    │
     │  │  (httpx)     │  │   Runner          │    │
     │  └──────────────┘  └───────────────────┘    │
     └─────────────────────────────────────────────┘
```

### Component overview

| Component | Location | Purpose |
|-----------|----------|---------|
| **CLI** | `localforge/cli/` | Typer-based CLI with 14 commands |
| **Agents** | `localforge/agent/` | 6 specialist agents + orchestrator |
| **Chat** | `localforge/chat/` | Interactive chat REPL with session persistence |
| **Core** | `localforge/core/` | Config, data models, Ollama client, Git utils, prompt templates |
| **Index** | `localforge/index/` | SQLite-backed code index + FTS5 search |
| **Retrieval** | `localforge/retrieval/` | Multi-strategy context retrieval + ranking |
| **Context Manager** | `localforge/context_manager/` | Token counting, budget allocation, prompt assembly |
| **Patching** | `localforge/patching/` | File patching with backup, rollback, fuzzy matching |
| **Verifier** | `localforge/verifier/` | Project detection + automated test/lint/type-check |

---

## Agent Loop

The orchestrator drives a multi-phase loop. Each phase uses a dedicated agent
with its own system prompt and structured JSON output schema.

```
                    ┌──────────────┐
                    │  User Task   │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  1. ANALYZE  │──── Understand the task & codebase
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  2. PLAN     │──── Produce ordered step list
                    └──────┬───────┘
                           ▼
               ┌───────────────────────┐
               │  For each plan step:  │
               │  ┌──────────────────┐ │
               │  │  3. CODE (patch) │ │
               │  └────────┬─────────┘ │
               │           ▼           │
               │  ┌──────────────────┐ │
               │  │  4. VERIFY       │ │
               │  └────────┬─────────┘ │
               │           │           │
               │     pass? │  fail?    │
               │      ▼    │    ▼      │
               │   [next]  │ ┌──────┐  │
               │           │ │REFLECT│  │
               │           │ └──┬───┘  │
               │           │    │      │
               │           │  retry    │
               │           │  (≤3x)    │
               └───────────────────────┘
                           ▼
                    ┌──────────────┐
                    │ 5. FINAL     │──── Full verification suite
                    │    VERIFY    │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │ 6. SUMMARIZE │──── Generate change summary
                    └──────────────┘
```

### Agent details

| Agent | Input | Output | Purpose |
|-------|-------|--------|---------|
| **Analyzer** | Task + retrieved code + repo structure | Structured analysis (understanding, files, complexity) | Understand what needs to be done |
| **Planner** | Analysis + code context | Ordered step list with file mappings | Break work into small, executable steps |
| **Coder** | Plan step + file content + context | Search/replace patch (exact or full-file for CREATE) | Write the actual code change |
| **Verifier** | Verification command output + step info | Pass/fail decision + next action | Interpret test/lint results |
| **Reflector** | Failed attempts + error history | Revised approach instructions | Learn from failures and suggest alternatives |
| **Summarizer** | All patches + verification results | Human-readable summary | Explain what was done |

Each step retry includes the Reflector agent, which analyzes the failure and
suggests a different approach. Maximum retries per step: **3**.

---

## How Retrieval Works

LocalForge uses a multi-strategy retrieval pipeline (not just keyword search):

1. **Query decomposition** — The task description is split into 3–5 focused
   sub-queries: quoted strings, snake_case identifiers, CamelCase names, file
   name hints, and significant keywords.

2. **Multi-strategy search** — For each sub-query:
   - **FTS5 lexical search** over indexed code chunks
   - **Filename fuzzy matching** using SequenceMatcher
   - **Symbol search** (function/class names) via SQL
   - **ripgrep** integration (if available) for regex matches

3. **Scoring and ranking** — Chunks are scored by:
   - Lexical relevance (FTS5 rank)
   - Term-frequency of query keywords in content
   - Path relevance (filename matches task keywords)
   - Recency (recently modified files get a boost)
   - Deduplication penalty (similar chunks are penalized)

4. **Token budget fitting** — Final chunks are greedily packed into the model's
   context window, with high-value chunks truncated rather than dropped entirely.

---

## Project Rules

Create `.localforge/rules.md` with your project conventions. These rules are
automatically injected into every agent's system prompt.

```markdown
# Project Rules

- Always use type hints in Python code
- Follow PEP 8 style guidelines
- Write docstrings for all public functions
- Use `pytest` for testing with descriptive test names
- Import ordering: stdlib, third-party, local (enforced by ruff)
- All API endpoints must have error handling
- Database queries must use parameterized statements
```

The agent will follow these rules when generating patches.

---

## Configuration Reference

All configuration lives in `.localforge/config.yml`. Run `localforge init` to
generate a starter file.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model_name` | `string` | `qwen2.5-coder:7b` | Ollama model tag |
| `ollama_base_url` | `string` | `http://localhost:11434` | Ollama HTTP API URL |
| `max_context_tokens` | `int` | `16384` | Max tokens in LLM context window |
| `max_iterations` | `int` | `50` | Max agent loop iterations |
| `repo_path` | `string` | `.` | Repository root path |
| `index_db_path` | `string` | `.localforge/index.db` | SQLite index location |
| `auto_approve` | `bool` | `false` | Auto-approve patches |
| `dry_run` | `bool` | `false` | Preview patches only |
| `log_level` | `string` | `INFO` | Logging level |
| `model_profile` | `string` | `small` | Profile: `small`, `medium`, `large` |

### Environment variable overrides

Variables prefixed with `LOCALFORGE_` override config values:

```bash
LOCALFORGE_MODEL_NAME=codellama:13b localforge autofix "fix the bug"
LOCALFORGE_MAX_CONTEXT_TOKENS=8192 localforge autofix "refactor"
```

### Model Profiles

| Profile | Context Window | Retrieval Limit | Chunk Size | Reasoning Depth |
|---------|---------------|-----------------|------------|-----------------|
| `small` | 8 192 | 5 | 512 | 2 |
| `medium` | 8 192 | 10 | 1 024 | 4 |
| `large` | 32 768 | 20 | 2 048 | 8 |

---

## Model Recommendations

LocalForge works with any Ollama-compatible model. Tested recommendations:

| Model | Size | Profile | Best For |
|-------|------|---------|----------|
| `qwen2.5-coder:7b` | 7 B | `small` | Fast iteration, simple fixes |
| `qwen2.5-coder:14b` | 14 B | `medium` | Good balance of speed and quality |
| `qwen2.5-coder:32b` | 32 B | `large` | Complex refactors, multi-file changes |
| `codellama:13b` | 13 B | `medium` | Strong at code generation |
| `deepseek-coder-v2:16b` | 16 B | `medium` | Excellent reasoning |
| `llama3.1:8b` | 8 B | `small` | General-purpose, good at planning |

**Tips:**

- Start with `qwen2.5-coder:7b` on `small` profile — it's fast and capable.
- Upgrade to a larger model only when you see plan quality issues.
- The `large` profile with a 32 B+ model gives the best results but requires
  significant VRAM (≥ 24 GB).
- Set `max_context_tokens` to match your model's actual context window for best
  results.

---

## Choosing and Switching Models

LocalForge gives you three ways to work with models:

### 1. List available models

See all models you have pulled in your Ollama instance:

```bash
localforge models
```

This displays all available models with a checkmark next to your current default.

### 2. Set your default model

Permanently change your default model for the project:

```bash
# Interactive selection (shows numbered list)
localforge set-model

# Direct selection
localforge set-model qwen2.5-coder:14b
```

This updates `.localforge/config.yml` so all future commands use the new model.

### 3. Override on a per-command basis

Use `--model` (or `-m`) flag on **any** command that uses the LLM:

```bash
# One-time override for different commands
localforge autofix "fix the bug" --model codellama:13b
localforge chat --model qwen2.5-coder:32b
localforge plan "refactor" --model llama3.1:8b
localforge patch "fix" --model deepseek-coder-v2:16b
localforge analyze "slow endpoint" --model qwen2.5-coder:14b
```

The flag takes precedence over your default config.

### Environment variable override

For scripting or CI/CD, use environment variables:

```bash
export LOCALFORGE_MODEL_NAME=qwen2.5-coder:14b
localforge autofix "fix the bug"
```

### Quick model switching workflow

```bash
# 1. See available models
localforge models

# 2. Try a model on one command
localforge autofix "small fix" --model llama3.1:8b

# 3. If you like it, make it the default
localforge set-model llama3.1:8b

# 4. All future commands use it
localforge autofix "bigger task"
```

---

## Workflow Examples

### Fix a bug

```bash
localforge init
localforge index
localforge autofix "fix the null pointer exception in UserService.get_by_id"
```

### Add a feature

```bash
localforge autofix "add pagination to the GET /users endpoint with page and limit query params"
```

### Targeted step-by-step

```bash
# 1. See what context the agent has
localforge analyze "refactor database module to use connection pooling"

# 2. Generate a plan (review before executing)
localforge plan "refactor database module to use connection pooling"

# 3. Execute step-by-step with manual approval
localforge patch "refactor database module to use connection pooling"

# 4. Or execute a single step
localforge patch "refactor database module to use connection pooling" --step 2

# 5. Verify the changes
localforge verify
```

### Dry-run (preview without changes)

```bash
localforge autofix "add error handling to all API endpoints" --dry-run
```

### With a different model

```bash
localforge autofix "optimize the database queries" --model qwen2.5-coder:32b --profile large
```

### Code exploration

```bash
# Search for functions related to auth
localforge search "authenticate" --mode symbol

# Find files matching a pattern
localforge search "database" --mode filename

# Full-text search
localforge search "connection pool" --mode text
```

### Chat with your codebase

```bash
# Start an interactive chat session
localforge chat

# In the chat:
# > How does the authentication flow work?
# > What would break if I changed the User model?
# > /context database connection pooling
# > Can you explain the retrieval pipeline?
```

### Undo changes

```bash
# List available backups
localforge rollback

# Restore to a specific backup
localforge rollback 20260403T143022

# Or use the diff command to review changes first
localforge diff
```

### Debug mode

```bash
# See exactly what the agent is doing
localforge --verbose autofix "fix the bug in auth.py"
```

---

## Safety & Backups

LocalForge is designed to be safe by default:

| Safety Feature | Description |
|----------------|-------------|
| **File backups** | Every file is backed up to `.localforge/backups/<timestamp>/` before any patch is applied |
| **Diff preview** | Every patch is displayed as a unified diff before application |
| **Confirmation prompt** | Patches require explicit `y` approval unless `--yes` is passed |
| **Dry-run mode** | Use `--dry-run` to preview all changes without writing anything |
| **Rollback** | `localforge rollback <timestamp>` restores any backup state |
| **Verification** | After patching, the agent runs lint, type-check, and tests automatically |
| **Patch validation** | Patches are validated for syntax correctness and scanned for dangerous patterns (eval, os.system, hardcoded secrets, etc.) |
| **Path traversal protection** | File paths are validated to stay within the repository root |
| **Iteration cap** | The agent stops after `max_iterations` (default: 50) to prevent runaway loops |
| **No network** | All processing happens locally via Ollama. Zero external network calls |

---

## Tips for Best Results

1. **Be specific in your task description.** Instead of "fix the bug", say "fix
   the null check in `UserService.get_by_id` that causes a crash when the user
   doesn't exist".

2. **Index frequently.** Run `localforge index` after major changes so the agent
   has fresh context. Incremental indexing is fast.

3. **Use `analyze` first.** Before running `autofix`, use `localforge analyze`
   to preview what code the agent will see. If the relevant code isn't in the
   results, adjust your task description.

4. **Start with `plan`.** For complex tasks, run `localforge plan` first to
   review the generated plan before execution.

5. **Use project rules.** Add your coding conventions to `.localforge/rules.md`
   — the agent quality improves significantly when it knows your standards.

6. **Match the profile to your model.** A `small` profile with a 7B model is
   faster and often sufficient. Only use `large` when the task genuinely needs
   a bigger context window.

7. **Review patches carefully.** Even with verification, AI-generated patches
   should be reviewed. The diff preview exists for a reason.

8. **Use `search` for exploration.** The `localforge search` command is a fast
   way to explore your codebase using the same index the agent uses.

---

## Limitations

LocalForge is alpha software. Known limitations:

| Limitation | Detail |
|------------|--------|
| **No semantic embedding search** | Retrieval is lexical + symbol-based. The embedding API exists but is not yet integrated into the search pipeline. |
| **No multi-repo support** | Operates on one repository at a time. |
| **LLM quality ceiling** | Output quality is bounded by the local model. Small models may produce incorrect patches for complex tasks. |
| **Context window pressure** | Very large files may be truncated to fit the token budget. Auto-detection helps, but some models still have small context windows. |
| **No runtime debugging** | The agent cannot set breakpoints or inspect runtime state. |

---

## Comparison with Cloud Agents

| Capability | LocalForge | Claude Code / Cursor |
|------------|------------|----------------------|
| **Privacy** | 100 % local, code never leaves machine | Code sent to cloud APIs |
| **Cost** | Free (uses your GPU) | Per-token or subscription billing |
| **Offline** | Fully offline | Requires internet |
| **Multi-agent** | 6 specialist agents with structured handoffs | Typically single-agent |
| **Interactive chat** | Yes (`localforge chat` with persistent history) | Yes |
| **Streaming output** | Yes (token-by-token) | Yes |
| **Git integration** | Auto-checkpoints before/after changes | Varies |
| **Code search** | SQLite FTS5 + symbols + ripgrep | Embedding-based or none |
| **Multi-language** | Python, JS/TS, Go, Rust, Java, C#, Ruby, PHP, C/C++ | Yes |
| **Safety** | Backup + diff + confirm + rollback + validation | Varies |
| **Token awareness** | Explicit budget management + auto-detect context window | May silently truncate |
| **Verification** | Auto-detects & runs pytest, ruff, mypy, npm test, go test | Usually manual |
| **Project rules** | `.localforge/rules.md` injected into all prompts | `.cursorrules` / CLAUDE.md |
| **Model quality** | Limited by local hardware (7B–70B) | GPT-4, Claude 3.5, etc. |

**Where LocalForge wins:** privacy, cost, offline use, transparency, safety
features, multi-agent architecture, token budget management, zero configuration.

**Where cloud agents win:** model quality (access to frontier models), semantic
search, larger context windows, faster inference on large models.

---

## Changelog

### v0.4.0

- **Tool-use from chat.** The chat engine now supports autonomous tool
  execution — the LLM can read files, write files, edit code, run shell
  commands, and search the codebase on its own, similar to Claude Code.
- **New slash commands.** `/run <cmd>` to execute shell commands, `/read <path>`
  to read files, `/tokens` to see session token usage — all from within chat.
- **Shell execution safety.** Destructive commands (`rm`, `del`, `format`, etc.)
  are blocked. Output is truncated at 20KB. Commands time out after 60 seconds.
- **Path traversal hardening.** Uses `Path.is_relative_to()` instead of string
  prefix checks for cross-platform safety.
- **Chat context window auto-detect.** The `chat` command now auto-detects the
  model's context window, matching the `autofix` command's behavior.
- **Resource cleanup fixes.** Async `ollama.close()` now runs in the same event
  loop for both `chat` and `patch` commands, preventing resource leaks.
- **Orchestrator path fix.** File reading during plan execution now correctly
  resolves repo-relative paths instead of relying on CWD.
- **Version sync fix.** `__init__.py` and `pyproject.toml` versions are now
  consistent.
- **New: 132 tests** (up from 109).
- Version bump to 0.4.0.

### v0.3.0

- **Interactive chat.** `localforge chat` — conversational REPL with codebase
  context retrieval, persistent chat history, and slash commands.
- **Streaming output.** Model responses now stream token-by-token to the
  terminal instead of showing only a spinner.
- **Auto-detect context window.** Queries the Ollama `/api/show` endpoint to
  determine the model's actual context window size automatically.
- **Git integration.** Automatic git checkpoints before and after `autofix`
  runs. The `status` command now shows git branch and changed files.
- **Enhanced symbol extraction.** Full support for Python (async, constants),
  JavaScript/TypeScript (const/let/var exports, interfaces, types, enums),
  Go (methods with receivers, struct/interface), Rust (pub fn, struct, enum,
  trait, impl), Java/Kotlin/C# (classes, interfaces, enums, methods),
  Ruby (modules), PHP, and C/C++.
- **Task history.** `localforge history` command shows previous autofix runs.
- **Patch safety.** The patcher now runs safety and syntax validation before
  applying patches, with interactive warnings for dangerous operations.
- **New: 109 tests** (up from 86).
- Version bump to 0.3.0.

### v0.2.0

- Added `localforge search` command (text, filename, symbol modes).
- Added `localforge rollback` command.
- Added `--verbose`/`--debug` flag with logging.
- Added project rules injection from `.localforge/rules.md`.
- Fixed: removed unnecessary Ollama check from `index` command.
- Fixed: converted smoke tests from inline scripts to proper pytest format.
- Comprehensive README.

### v0.1.0

- Initial release with full multi-agent pipeline.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

```bash
# Development setup
git clone https://github.com/localforge/localforge.git
cd localforge
pip install -e ".[dev]"

# Run tests
python -m pytest tests/ -v

# Lint
ruff check .

# Type check
mypy localforge/
```

---

## License

MIT — see [LICENSE](LICENSE).
