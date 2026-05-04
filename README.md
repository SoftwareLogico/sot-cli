# sot-cli 🚀 Limitless Local AI Agent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Providers](https://img.shields.io/badge/Providers-OpenRouter%20%7C%20LMStudio%20%7C%20OpenAI%20%7C%20Ollama%20%7C%20NVIDIA-brightgreen.svg)](ARCHITECTURE.md)
[![Stars](https://img.shields.io/github/stars/softwarelogico/sot-cli?style=social)](https://github.com/softwarelogico/sot-cli)
[![License](https://img.shields.io/github/license/SoftwareLogico/sot-cli?style=flat&logo=mit)](LICENSE)

## ▶️ Watch it Work (Video)

<p align="center">
    <a href="https://youtu.be/9h20O_aH6vs" title="sot-cli in action: True AI OS Control (Zero Guardrails)">
    <img src="https://img.youtube.com/vi/9h20O_aH6vs/maxresdefault.jpg" alt="▶️ sot-cli Demo — Click to watch the video" />
  </a>
  <br><strong>🎥 Click the thumbnail above to watch the demo video</strong>
</p>

**A pragmatic, limitless, multi-provider terminal assistant built for developers who hate bloated frameworks.**

`sot-cli` is a limitlessly local Python CLI designed to unleash the true reasoning power of modern LLMs on your projects. By combining a novel architectural pattern called the **Source of Truth (SoT) Method** with aggressive multi-tool batching, it drastically reduces API costs and model iterations while keeping output quality pristine. It acts as a powerful orchestration engine, empowering your AI with local tools and asynchronous sub-agents to solve complex problems seamlessly.

The name `sot-cli` is a direct nod to the architectural pattern it is built around — the **Source of Truth (SoT) Method** — and is intentionally unique so it does not get lost in the sea of generic AI tooling names.

## ✨ Key Features

- **📊 SoT Method**: Fresh files from disk every turn. No token bloat, always up-to-date.
- **🤖 Async Multi-Agent**: Delegate trial-and-error to cheap sub-agents (empty ctx).
- **⚡ Batch Orchestration**: Multi-tools + bash/Python scripts in ONE turn.
- **🔧 Full Tools**: 19 built-in (incl. unrestricted shell, regex code search, batched multi-file surgical edits) + MCP extensible.
- **🌐 Multi-Provider**: Switch OpenRouter/LMStudio/OpenAI/Ollama/NVIDIA live.
- **💰 Native Prompt Caching**: Payload architecture designed for prefix-matching, saving up to 50% API costs on long histories by caching static dialogue and keeping dynamic files at the bottom.
- **🧠 Context Awareness**: Real-time context limit tracking (Allocated vs. Max) with visual terminal warnings to prevent token overflow.

👉 [SoT](SoT_Method.md) | [Tools](ARCHITECTURE.md) | [Roadmap](ROADMAP.md)

## Platform Compatibility

- ✅ **macOS**: Fully tested and compatible.
- ✅ **Windows**: Fully tested and compatible.
- ✅ **Linux**: Fully tested and compatible.

### Clone the repo

```bash
git clone https://github.com/SoftwareLogico/sot-cli.git
cd sot-cli
```

## 🚀 How to Run

### Create and activate a virtual environment (Optional but recommended)

```bash
#uv
uv venv <env_name> --python 3.10
source <env_name>/bin/activate
uv pip install -e .
uv run sot-cli

#conda
conda create -n <env_name> python=3.10
conda activate <env_name>
pip install -e .
sot-cli

#venv
python3 -m venv <env_name>
source <env_name>/bin/activate
pip install -e .
sot-cli
```

### Install dependencies

```bash
pip install -e .
```

### Run sot-cli

```bash
sot-cli
```

Follow the steps the first time, have Fun!!

## 🛠 Manual Installation

If you would rather wire things up by hand instead of going through the first-run wizard,
After cloning and installing dependencies (see [How to Run](#-how-to-run)), follow the steps below.

### Rename TOML files

- 🟨 `sot.example.toml` => 🟩 `sot.toml`
- 🟨 `sot.keys.example.toml` => 🟩 `sot.keys.toml`

These files are already in `.gitignore`, so your secrets will never be committed.

### API Compatibility

sot-cli is compatible with any OpenAI‑compatible (OpenAI‑like) API. The following providers have been tested and verified:

- ✅ OpenRouter
- ✅ LM Studio (local)
- ✅ OpenAI (and any OpenAI-compatible API behind the same `openai` provider name)
- ✅ Ollama (local)
- ✅ NVIDIA

We will continue adding and testing more providers — contributions welcome.

### Add API keys

Edit `sot.keys.toml` and fill in the providers you intend to use. Local providers (`lmstudio`, `ollama`) usually leave the key empty.

```toml
[providers.openrouter]
api_key = "sk-or-v1-your-key-here"

[providers.lmstudio]
# Usually doesn't need an API key for local models
api_key = ""

[providers.openai]
# Optional — leave empty for OpenAI-compatible local servers that don't require a key.
api_key = "sk-..."

[providers.ollama]
# Usually doesn't need an API key for local models
api_key = ""

[providers.nvidia]
api_key = "nvapi-your-key-here"
```

### Configure providers

Edit `sot.toml` to set base URLs, models, and per-provider runtime options.

```toml
[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
model = "x-ai/grok-4.1-fast"
temperature = 0.7
max_output_tokens = 32768
reasoning_effort = "medium" # options: "none" | "minimal" | "low" | "medium" | "high" | "xhigh" — silently ignored by non-reasoning upstreams

[providers.lmstudio]
base_url = "http://localhost:1234/v1"
model = "" # empty means it'll use the loaded one
temperature = 0.7
max_output_tokens = 32768

[providers.openai] # works with OpenAI and any OpenAI-compatible API
base_url = "https://api.openai.com/v1"
model = "gpt-5.4-mini-2026-03-17" # required — set to your served model name
temperature = 0.7
max_output_tokens = 32768

[providers.ollama]
base_url = "http://localhost:11434/v1"
model = "" # empty means it'll use the loaded one
temperature = 0.7
max_output_tokens = 32768

[providers.nvidia]
base_url = "https://integrate.api.nvidia.com/v1"
model = "qwen/qwen3-coder-480b-a35b-instruct"
temperature = 0.7
max_output_tokens = 32768
```

For full per-provider field semantics (including OpenAI-specific quirks like `max_completion_tokens` and tool schema sanitization), see [ARCHITECTURE.md → Provider configuration](ARCHITECTURE.md#provider-configuration-providersx).

### Run the CLI with the most common parameters

```bash
# RECOMMENDED: Use the default provider set in sot.toml (or pick from the interactive selector)
sot-cli

# Or override the provider explicitly
sot-cli --provider [openrouter/lmstudio/openai/ollama/nvidia]
# e.g. sot-cli --provider openai

sot-cli --provider [openrouter/lmstudio/openai/ollama/nvidia] --model modelName
# e.g. sot-cli --provider openai --model gpt-5.4-mini-2026-03-17

# List all sessions as JSON (no AI round-trip, reads straight from disk)
sot-cli --list_sessions

# Use a different model for delegated sub-agents
sot-cli prompt --subagent_model gemma4

# Resume a previous session
sot-cli <session_id>

# Cleaning the house removing extras manually
# Manually remove files in SoT
sot-cli --clean_sot <session_id>

# Convert previous used tools into receipts
sot-cli --clean_sot <session_id> --hypercompress
```

## 🧠 The Core Concept: The SoT Method

Most AI coding agents fail because they append every file read and every code change directly into the chat history. This leads to massive token bloat and "Lost in the Middle" hallucinations where the AI reads an outdated version of a file from 10 turns ago.

`sot-cli` fixes this by separating **Permanent History** from **Ephemeral State**.

1. **Permanent History (`chat_history`):** Only contains dialogue and lightweight tool metadata (e.g., `"read file X -> added to SoT"`).
2. **Ephemeral Source of Truth (SoT):** This method tracks the latest state of your context files so the model always reads the most up-to-date version, and not 10 different versions of the same file from the chat history. When the model uses a tool to read or edit a file, the SoT updates that file's content. The model can then refer to the SoT for the latest state of any file, without bloating the chat history.

**Smart Token Economy (Permanent vs. Ephemeral):**
You can attach core files (like database schemas or project guidelines) permanently to a session so the AI _always_ knows them. Meanwhile, files the AI reads to fix a specific bug are treated as "ephemeral"—they stay in the SoT while needed, and can be detached immediately after the bug is fixed to keep your token usage incredibly low.

**Result:** The model always sees the absolute latest state of your project. Context grows linearly, not exponentially. Furthermore, because the dynamic SoT block is injected at the _bottom_ of the payload, it perfectly exploits **Prefix-Matching Prompt Caching**, keeping your long conversation histories 100% cached and drastically reducing API costs.
👉 [Read the full SoT Method explanation here.](SoT_Method.md)

## 🧪 Benchmarks

Optional benchmark suite for post-launch validation.

- ✅ [agent_test.md](prompt_tests/agent_test.md): Safe end-to-end benchmark. It validates parallel sub-agent orchestration, file download and verification, local file create/edit flow, native OS command execution, fallback/retry behavior, and final cleanup/reporting.
- ⚠️ [seppuku_test.md](prompt_tests/seppuku_test.md): Intentionally destructive lab benchmark used to demonstrate raw model power without babysitting or guardrails.

⚠️WARNING: [seppuku_test.md](prompt_tests/seppuku_test.md) is for isolated lab VM use only .⚠️

## 💸 Token Economy: Scripts > Tool Ping-Pong

We hate "Tool Ping-Pong" (when an AI calls `list_dir`, waits, calls `read_file`, waits, calls `grep`, waits). It burns hundreds of thousands of context tokens.

`sot-cli` is designed to batch operations. The system prompts drive the model to use `run_command` for bash one-liners or Python mini-scripts, `list_dir` for powerful filtered discovery (by name, extension, size, content), and `search_code` for regex pattern matching with line numbers across source files — all in a single turn.

Why use 5 sequential tool calls when the model can batch `list_dir` + `search_code` + `read_files` (with all known paths in one array) in one response?

## 🛑 The Anti-Hype FAQ

If you are coming from other trendy AI coding tools, you might be looking for features that we intentionally excluded. Here is why:

### "Where is my `CLAUDE.md` / `rules` file?"

**It's a gimmick.** You don't need a hardcoded framework feature to make an AI read rules. If you have a project guidelines file, just tell the agent: _"Read `guidelines.md` and follow it."_ The agent will add it to the SoT and obey it. We don't hardcode magic filenames.

### "Where are my 'Skills'?"

**A 'Skill' is just a glorified preprompt.** We don't bloat the codebase with fake "skills" (e.g., a React Skill, a Docker Skill). Modern LLMs already know React and Docker. If they need to do something specific, they can write a bash or python script via `run_command` on the fly.

### "Why is there no Context Compaction / Summarization?"

**Because it causes lobotomies.** Summarizing past turns makes the model forget crucial details. By using the SoT Method, our `chat_history` only contains metadata and dialogue. It grows so slowly that you will likely finish your task long before hitting the 200k token limit.

### "Where are the Slash Commands (`/clear`, `/file`)?"

**This is an autonomous agent, not a basic chatbot.** If the model needs a file, it uses a tool to read it. You shouldn't be manually typing commands to manage its context.

---

## 🤖 Asynchronous Multi-Agent Orchestration

`sot-cli` supports a Boss-Worker delegation model using Just-In-Time (JIT) sub-agents.

If your main SoT is heavily loaded (expensive context), the main agent can use `delegate_task` to spawn a sub-agent in the background with a clean, empty context.
The sub-agent does the dirty work (trial-and-error shell scripts, complex multi-step execution, compiling), logs everything silently to `agent.log`, and returns a clean report to the Boss via invisible IPC. For file discovery and code search, the Boss can use `list_dir` and `search_code` directly — cheaper than spawning a sub-agent.

The Boss orchestrates. The Workers execute. Your terminal stays clean.

For full agent/sub-agent command reference (including CLI flags and orchestration tool parameters), see [ARCHITECTURE.md](ARCHITECTURE.md#agent-and-sub-agent-commands--parameters).

---

## 🧰 Available Tools

For the complete and up-to-date tool and parameter reference, see [ARCHITECTURE.md](ARCHITECTURE.md).

## ⚙️ Runtime Configuration

All runtime settings live in `sot.toml` under `[tools]`. In a nutshell:

- **Detection & limits:** `binary_check_size`, `default_command_timeout_seconds`.
- **Streaming visibility:** `show_thinking` (model reasoning), `show_full` (tool call arguments in real time).
- **Loop limits:** `max_rounds` (boss), `delegated_max_rounds` (sub-agent), `repeat_limit` / `delegated_repeat_limit` (abort on identical consecutive rounds).
- **Reasoning budget:** `reasoning_char_budget` (boss), `delegated_reasoning_char_budget` (sub-agent) — hard cap on streamed reasoning characters per turn. Set it to `0` to disable.

For the full reference table with defaults and descriptions, and see [ARCHITECTURE.md](ARCHITECTURE.md#session-and-config-notes-for-orchestration).

## 🔌 MCP Servers

You can easily extend `sot-cli` with external tools using the Model Context Protocol (MCP). Just add them to your `sot.toml`:

```toml
[mcp.servers.test]
command = "python"
args = ["mcps/test.py"]
```

The runtime will automatically start the server and expose its tools to the AI.

---

### ⚠️ WARNING: No Guardrails. No Policies.

This tool is **limitless** by design. It is not built for end-users; it is built for power users. It really can do anything you ask as well as is within the capabilities of your system. It does not have a babysitter checking its actions. **It will execute what you tell it to execute without hesitation.** The internal `JB_SYSTEM_PROMPT` actively forces the model to bypass safety filters to allow administrative and destructive lab testing. Use it responsibly.

## 🌟 Star & Contribute

⭐ **Star if it saves your API bill!** [Star Here](https://github.com/softwarelogico/sot-cli)

- 🐛 PR/issues welcome (see [ROADMAP](ROADMAP.md)).
- 📢 Share: "sot-cli: AI agent without token waste #AICoding"

## 👤 Author & Credits

**Created by Ramses Mendoza (SoftwareLogico)**

I built `sot-cli` and formalized the **Source of Truth (SoT) Method** for terminal agents out of frustration with existing tools. Most AI coding assistants on the market are bloated, burn through tokens, and collapse under the weight of their own context windows.

While the concept of maintaining a "state" is common in software engineering, the specific architectural pattern of decoupling a permanent metadata-only history from an ephemeral, fully-rebuilt file block—and injecting it right before the user prompt—is the core innovation of `sot-cli`.

**LinkedIn:** https://www.linkedin.com/in/ramsesisaid

This tool was designed for absolute power, raw speed, and extreme token efficiency, **since it follows no agenda other than being truly useful.** It doesn't babysit you, it doesn't enforce corporate safety rails on your local machine, and it doesn't waste your API credits on unnecessary framework overhead.
