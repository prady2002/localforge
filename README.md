# LocalForge

**A local-first, repo-aware coding agent powered by Ollama.**

LocalForge is a fully offline, privacy-first AI coding agent that lives in your
terminal. Point it at a codebase, describe a task in plain English, and it will
analyze the code, build an execution plan, generate patches, run verification,
and iterate — all using a local LLM through [Ollama](https://ollama.com).

**Why LocalForge?**

- **100 % local.** Your code never leaves your machine. No API keys, no cloud,
  no telemetry.
- **Repo-aware.** A SQLite-backed index with lexical, filename, and symbol
  search gives the agent targeted context instead of brute-forcing the entire
  tree into a context window.
- **Multi-agent architecture.** Six specialist agents (Analyzer → Planner →
  Coder → Verifier → Reflector → Summarizer) collaborate through structured
  handoffs, each with its own system prompt and JSON schema.
- **Token-budget-aware.** A dedicated context assembler keeps every prompt
  within the model's context window — no silent truncation surprises.
- **Safe by default.** Every patch is shown as a diff, backed up before
  application, and only written after explicit approval (or `--yes`).

---

## Table of Contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Quick Start](#quick-start)
4. [CLI Commands](#cli-commands)
5. [Architecture](#architecture)
6. [Agent Loop](#agent-loop)
7. [Configuration Reference](#configuration-reference)
8. [Model Recommendations](#model-recommendations)
9. [Safety](#safety)
10. [Limitations](#limitations)
11. [Contributing](#contributing)
12. [License](#license)

---

## Requirements

| Dependency | Version | Notes |
|------------|---------|-------|
| Python | **3.11 +** | 3.12 works too |
| [Ollama](https://ollama.com) | latest | Must be running (`ollama serve`) |
| ripgrep (`rg`) | *optional* | Speeds up file discovery if installed |
| Git | *optional* | Used for change tracking |

---

## Installation

```bash
pip install localforge
```

Or install from source:

```bash
git clone https://github.com/localforge/localforge.git
cd localforge
pip install -e ".[dev]"
```

---

## Quick Start

Five commands to go from zero to an automated fix:

```bash
# 1. Install Ollama and pull a model
ollama pull qwen2.5-coder:7b

# 2. Initialize localforge in your project
cd your-project/
localforge init

# 3. Index the codebase (one-time, ~seconds for most repos)
localforge index

# 4. Run an analysis to see what the agent finds
localforge analyze "fix the authentication bug in the login endpoint"

# 5. Run the full autofix pipeline
localforge autofix "fix the authentication bug in the login endpoint"
```

That's it. LocalForge will analyze the code, plan the fix, generate patches,
run verification, and iterate until the task is done — all locally.

---

## CLI Commands

### `localforge init`

Initialize a `.localforge/` directory with default configuration files.

```bash
localforge init                 # current directory
localforge init /path/to/repo   # specific repo
```

Creates: `config.yml`, `rules.md`, `commands.yml`.

---

### `localforge index`

Build or refresh the SQLite code index for fast retrieval.

```bash
localforge index                # incremental update
localforge index --force        # full re-index
localforge index --repo ./myapp
```

| Flag | Description |
|------|-------------|
| `--force` | Re-index all files from scratch |
| `--repo`, `-r` | Path to the repository root (default: `.`) |

---

### `localforge analyze`

Retrieve the most relevant code chunks for a given task description.

```bash
localforge analyze "why is the login endpoint slow?"
localforge analyze "add pagination to the users API" --limit 20
```

| Flag | Description |
|------|-------------|
| `--limit`, `-n` | Max chunks to retrieve (default: `10`) |
| `--repo`, `-r` | Path to the repository root |

---

### `localforge plan`

Run analysis and produce an execution plan (saved to `.localforge/last_plan.json`).

```bash
localforge plan "add input validation to the signup form"
```

| Flag | Description |
|------|-------------|
| `--repo`, `-r` | Path to the repository root |

---

### `localforge patch`

Generate and apply code patches from a saved plan.

```bash
localforge patch "add input validation to the signup form"
localforge patch "fix bug" --step 2          # execute only step 2
localforge patch "fix bug" --dry-run         # preview without writing
localforge patch "fix bug" --yes             # auto-approve all patches
```

| Flag | Description |
|------|-------------|
| `--step`, `-s` | Execute only this step number |
| `--dry-run` | Show patches without applying |
| `--yes`, `-y` | Auto-approve all patches |
| `--repo`, `-r` | Path to the repository root |

---

### `localforge verify`

Run the project's verification suite (lint, type-check, tests).

```bash
localforge verify
localforge verify --repo ./myapp
```

Auto-detects: **pytest**, **ruff**, **mypy**, **npm test**, **go test**.

---

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
| `--dry-run` | Show patches without applying |
| `--model`, `-m` | Override the Ollama model |
| `--profile`, `-p` | Model profile: `small`, `medium`, `large` |
| `--max-iterations` | Override max agent iterations |
| `--repo`, `-r` | Path to the repository root |

---

### `localforge status`

Show project status: index stats, Ollama health, model info, last task.

```bash
localforge status
```

---

### `localforge diff`

Show unified diffs for changes made by localforge (uses the backup system).

```bash
localforge diff                       # latest backup
localforge diff 20260403_143022       # specific timestamp
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      CLI (Typer)                        │
│  init │ index │ analyze │ plan │ patch │ verify │ autofix│
└──────────────────────┬──────────────────────────────────┘
                       │
          ┌────────────▼────────────┐
          │   AgentOrchestrator     │
          │  (coordinates pipeline) │
          └────┬───┬───┬───┬───┬───┘
               │   │   │   │   │
    ┌──────────┘   │   │   │   └──────────┐
    ▼              ▼   ▼   ▼              ▼
┌────────┐  ┌────────┐ ┌────────┐  ┌────────────┐
│Analyzer│  │Planner │ │ Coder  │  │  Verifier  │
└────────┘  └────────┘ └────────┘  └────────────┘
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

Each step retry includes the Reflector agent, which analyzes the failure and
suggests a different approach. Maximum retries per step: **3**.

---

## Configuration Reference

All configuration lives in `.localforge/config.yml`. Run `localforge init` to
generate a starter file.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model_name` | `string` | `qwen2.5-coder:7b` | Ollama model tag |
| `ollama_base_url` | `string` | `http://localhost:11434` | Ollama HTTP API URL |
| `max_context_tokens` | `int` | `4096` | Max tokens in LLM context window |
| `max_iterations` | `int` | `50` | Max agent loop iterations |
| `repo_path` | `string` | `.` | Repository root path |
| `index_db_path` | `string` | `.localforge/index.db` | SQLite index location |
| `auto_approve` | `bool` | `false` | Auto-approve patches |
| `dry_run` | `bool` | `false` | Preview patches only |
| `log_level` | `string` | `INFO` | Logging level |
| `model_profile` | `string` | `small` | Profile: `small`, `medium`, `large` |

Environment variables override config with the `LOCALFORGE_` prefix:

```bash
LOCALFORGE_MODEL_NAME=codellama:13b localforge autofix "fix the bug"
```

### Model Profiles

| Profile | Context Window | Retrieval Limit | Chunk Size | Reasoning Depth |
|---------|---------------|-----------------|------------|-----------------|
| `small` | 4 096 | 5 | 512 | 2 |
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

## Safety

LocalForge is designed to be safe by default:

- **Backups.** Every file is backed up to `.localforge/backups/<timestamp>/`
  before any patch is applied.
- **Diff preview.** Every patch is displayed as a unified diff before
  application.
- **Confirmation prompt.** Patches require explicit `y` approval unless
  `--yes` is passed.
- **Dry-run mode.** Use `--dry-run` to preview all changes without writing
  anything.
- **Verification.** After patching, the agent runs lint, type-check, and tests
  automatically to catch regressions.
- **Iteration cap.** The agent stops after `max_iterations` (default: 50) to
  prevent runaway loops.
- **No network.** All processing happens locally via Ollama. Your code is never
  sent to any external service.

---

## Limitations

LocalForge is alpha software. Known limitations:

- **No semantic embedding search.** Retrieval is lexical + symbol-based. It
  works well for targeted queries but may miss semantic connections.
- **Single-language focus.** Best results with Python codebases. Other
  languages are indexed and patchable but less thoroughly tested.
- **No multi-repo support.** Operates on one repository at a time.
- **LLM quality ceiling.** Output quality is bounded by the local model. Small
  models may produce incorrect patches for complex tasks.
- **No interactive debugging.** The agent cannot set breakpoints or inspect
  runtime state.
- **No git integration for rollback.** Backups are file-based, not
  commit-based. Use git for robust version control.
- **Context window pressure.** Very large files may be truncated to fit the
  token budget. The `large` profile helps but doesn't eliminate this.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## License

MIT — see [LICENSE](LICENSE).
