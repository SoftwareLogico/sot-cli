# Current architecture of sot-cli

## Runtime objective

`sot-cli` is a local terminal assistant that delegates generation to the provider, but maintains control of the operational state on the runtime side:

- local sessions and transcripts on disk;
- local tools for files, shell and session control;
- Source of Truth (SoT) rebuilt by the runtime;
- permanent conversation history (`chat_history`) separated from ephemeral SoT;
- conversation state always controlled locally by the runtime; provider-side sessions are not used;
- independence from the wire format of the provider behind adapters.

The central rule is this:

> The provider is not the source of truth of the project; the source of truth is the local state that the runtime reinjects into the model when appropriate.

The CLI is named after this exact rule: **SoT** (Source of Truth) is the architectural keystone, and `sot-cli` is the tool that implements it end-to-end.

## Startup modes

### `prompt` (main mode)

Creates or resumes an interactive session in terminal. Detects model capabilities from the provider API (or applies optimistic assumed defaults for `openai`, see "Provider configuration" below) and displays them in the startup banner. Creates a `SoTState` that lives in memory during the entire process, accumulating `chat_history` between turns.

The startup banner shows: provider, session id, route (base URL), model, start timestamp, capability flags, host environment (OS/arch/hostname/user/shell/cwd), and a magenta `tip:` line reminding the user that `sot.toml` and `sot.keys.toml` are the source of truth for provider settings (no need to re-run the wizard for tweaks).

### `command` (one-shot mode for multi-agent)

Executes a single turn against an existing session. Creates a fresh `SoTState` per invocation. Designed as the primitive for multi-agent orchestration.

### `run_task` (headless agent execution)

Executes a headless agent session. Used internally by the `delegate_task` tool to spawn sub-agents in isolated environments.

## Agent and Sub-agent Commands & Parameters

This section centralizes the parameters that are commonly omitted in high-level docs.

### Global CLI flags

These flags work with any command or on their own:

| Flag | Description |
|------|-------------|
| `--config <path>` | Path to `sot.toml` config file. |
| `--list_sessions` | Dump all sessions as JSON to stdout. Does not require a subcommand. No AI round-trip — read directly from disk. |
| `--clean_sot <session_id>` | Remove ALL tracked SoT files from a session. Both permanently-attached and ephemerally-read files are cleared. No AI round-trip. |

Example output:

```
SESSIONS:
[
  {
    "id": "20260430-112040",
    "title": "prompt 2026-04-30 11:20:40",
    "provider": "openrouter",
    "model": "openrouter/owl-alpha",
    ...
  }
]
```

### CLI commands used to run agents

#### `sot-cli prompt [session_id]`

Interactive main loop (Boss agent context).

Supported flags:

- `--title <text>`: set title for a new session.
- `--provider <lmstudio|openrouter|openai|ollama|nvidia>`: provider override.
- `--model <name>`: model override.
- `--no-tools`: disables tool loop (plain chat behavior).

Examples:

- `sot-cli prompt`
- `sot-cli prompt <session_id> --provider openrouter --model x-ai/grok-4.1-fast`

#### `sot-cli chat [session_id]`

Alias of `prompt`, same parameters and behavior.

#### `sot-cli command <session_id> <prompt>`

One-shot turn runner (automation/multi-agent primitive).

Supported flags:

- `--provider <lmstudio|openrouter|openai|ollama|nvidia>`
- `--model <name>`
- `--no-tools`
- `--disable-delegation`: removes `delegate_task` from available tools to avoid recursion loops.

Examples:

- `sot-cli command <session_id> "Analyze this repo and propose fixes"`
- `sot-cli command <session_id> "Run tests" --provider openrouter --disable-delegation`

#### `sot-cli run_task <agent_id> <prompt>`

Executes a headless sub-agent (Worker), typically created as `agent_N`.

Examples:

- `sot-cli run_task agent_1 "Search all TODOs and return a summary"`

### Tool commands used for sub-agent orchestration

These are runtime tools the Boss model uses during a turn.

#### `delegate_task`

Parameters:

- `task_prompt` (required): detailed instructions for the Worker.
- `provider` (optional): provider override for the Worker.
- `attempts` (optional, integer, min 1): max repeated failed attempts before abort. Default `2`.
- `background` (optional, boolean): default `false`. With `false` the call blocks until the Worker exits, then `wait_task` returns the report immediately. With `true` the call returns instantly with the `agent_id` so the Boss can fan out additional delegations in the same round; `wait_task` on each one then blocks until that specific Worker finishes. Use `true` only when launching multiple delegations in parallel — a single delegation with `background=true` followed by `wait_task` is strictly worse than `background=false`.

#### `wait_task`

Parameters:

- `agent_id` (required): target sub-agent id (e.g. `agent_1`). Always blocks until the Worker writes its `response.md`; the runtime detects loops, exhausted budgets, length truncation and silent provider failures and forces the response.md to be written with a clear error status, so an unbounded wait cannot stall.

#### `list_tasks`

No parameters. Returns delegated tasks and status (RUNNING/COMPLETED). Prefer `wait_task` instead of polling loops.

### Session and config notes for orchestration

- Global CLI flag `--config <path>` can be passed before commands to use a specific TOML config.
- Delegated agents inherit runtime context and session storage rules (including `SOT_SESSIONS_DIR` when set).
- Boss/Worker prompt separation is enforced by the runtime (`AGENT_SYSTEM_PROMPT` vs `SUB_AGENT_SYSTEM_PROMPT`).
  All runtime tool settings live in `[tools]` inside `sot.toml`. The values listed below are the **code defaults** applied when the field is absent; the bundled `sot.example.toml` ships with looser values intended as a comfortable starting point for real workloads.

| Setting                             | Default | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ----------------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `default_command_timeout_seconds`   | `180`   | Hard wall-clock timeout (seconds) applied to every `run_command` invocation. The model cannot override it per call — the limit is a property of the runtime, not a parameter. Raise it only if the host's legitimately slow operations consistently exceed three minutes.                                                                                                                                                                                                                                                                                                       |
| `binary_check_size`                 | `0`     | Byte threshold used by `read_files` to detect binary content. The reader inspects this many bytes at the start of the file and rejects non-UTF8 input above the threshold, preventing the SoT from being polluted with garbage bytes that would also balloon the context. Set to `0` to skip content-based binary detection — all files are treated as text (known binary extensions like `.png` are still blocked).                                                                                                                                                                                                                                                                                                        |
| `show_thinking`                     | `true`  | Stream the model's reasoning/thinking tokens to the terminal as they arrive. Independent of `show_full`; gates **reasoning** output only. Set to `false` if the live reasoning trace is noisy — the model still reasons internally, you just don't see it scroll.                                                                                                                                                                                                                                                                                                                |
| `show_full`                         | `true`  | Stream tool-call argument chunks (and any other non-reasoning, non-text chunk the provider emits) in real time as the model generates them, verbatim. When disabled, tool calls are only shown as a single assembled line after streaming completes. Provider chunks are never mutated by the runtime; `show_full` only toggles whether they are rendered live.                                                                                                                                                                                                                  |
| `max_rounds`                        | `25`    | Max tool-call rounds the boss agent can execute per user prompt before the runtime stops it. The bundled example raises this to `250` for power users.                                                                                                                                                                                                                                                                                                                                             |
| `delegated_max_rounds`              | `8`     | Max tool-call rounds a sub-agent can execute before being stopped. The bundled example raises this to `80`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `repeat_limit`                      | `3`     | Max consecutive identical rounds (same tools, same arguments) the boss can repeat before the runtime aborts with a loop warning. Set to `0` to disable repeat detection.                                                                                                                                                                                                                                                                                                                                                                                                  |
| `delegated_repeat_limit`            | `2`     | Same as above but for sub-agents. Tighter on purpose because workers are expected to converge faster on a narrower task. Set to `0` to disable.                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `search_default_head_limit`         | `200`   | Default max number of result entries returned by a single `search_code` call when the model does not pass an explicit `head_limit`. Set to `0` for unlimited by default.                                                                                                                                                                                                                                                                                                                                                                                                      |
| `search_max_line_length`            | `500`   | Per-line truncation length (characters) applied to `search_code` output. Lines longer than this are trimmed in the results to save context. Set to `0` to disable truncation — the model sees full lines.                                                                                                                                                                                                                                                                                                                               |
| `search_timeout_seconds`            | `30`    | Hard timeout (seconds) for a single `search_code` invocation (ripgrep subprocess or Python fallback). Keeps pathological patterns from hanging a turn.                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `reasoning_char_budget`             | `0`     | Hard cap on streamed reasoning/thinking characters per turn for the boss agent. When the cumulative reasoning channel exceeds this budget during a single stream, the runtime cuts the provider connection, prints a yellow `⚠  reasoning budget exceeded` warning, and lets the tool loop advance. Protects against models that get stuck in eternal "let me reconsider…" loops inside a single response. Set to `0` to disable the cap.                                                                                                                                        |
| `delegated_reasoning_char_budget`   | `0`     | Same cap applied to sub-agent turns. Typically smaller than the boss budget because delegated workers are expected to execute narrow tasks and should not spend long reasoning windows before acting. Set to `0` to disable.                                                                                                                                                                                                                                                                                                                                                     |
| `compression_reasoning_trunc_chars` | `240`   | Hard cap (chars) on the `reasoning` and the merged `reasoning_details` text of any tool-bearing assistant message in CLOSED turns when the outbound payload is built. Same cap is also used to clip the reasoning excerpt embedded in the `SYSTEM MESSAGE:` line that replaces successful `write_file` / `edit_files` pairs in old turns. The reasoning of the final user-facing assistant message of a closed turn (no `tool_calls`) is never truncated. The active turn is never compressed at all. Set to `0` to disable the cap (full reasoning round-trips for every turn). |

## Provider configuration (`[providers.X]`)

`sot-cli` ships with a single OpenAI-compatible HTTP adapter (`sot_cli/providers/openai_compat.py`) that talks to every supported backend. Per-provider tweaks live under `[providers.<name>]` in `sot.toml`; per-provider API keys live under `[providers.<name>] api_key = "..."` in `sot.keys.toml` (separate file so `.gitignore` can keep secrets out of commits).

Every provider section accepts the same five base fields:

| Field               | Type            | Description                                                                                                                                                                                                                                              |
| ------------------- | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `base_url`          | string          | Full URL of the OpenAI-compatible endpoint, including the `/v1` suffix.                                                                                                                                                                                  |
| `model`             | string          | Model name. Required for cloud providers. For `lmstudio` and `ollama` it can be left empty (`""`) so the adapter auto-resolves the currently loaded model.                                                                                               |
| `temperature`       | float           | Sent on every request (subject to per-provider quirks — see below).                                                                                                                                                                                      |
| `max_output_tokens` | int             | Token cap on the completion. Wire-level field name diverges per provider; the adapter handles the rename.                                                                                                                                                |
| `configured`        | bool (optional) | Marker written by the wizard. When `true`, the selector skips the per-provider mini-wizard and enters the session directly. Manual edits to provider settings do **not** require flipping this back to `false` — the runtime trusts whatever is on disk. |

### Optional fields per provider

| Field                       | Where it applies       | Effect                                                                                                                                                                                                                                                                             |
| --------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reasoning_effort`          | `openrouter`           | OpenRouter uses nested `"reasoning": {"effort": "<level>"}`. Silently ignored on non-reasoning upstreams. Accepted: `"none"`-`"xhigh"`.                                                                                       |
| `http_referer`, `app_title` | `openrouter`           | Forwarded as `HTTP-Referer` and `X-OpenRouter-Title` headers (used by OpenRouter for app attribution and rankings).                                                                                                                                                                |

### Supported provider names

`lmstudio`, `openrouter`, `openai`, `ollama`, `nvidia`. New names can be added to `KNOWN_PROVIDERS` in `sot_cli/config/app.py`; the same OpenAI-compatible adapter handles them all. Note: OpenAI's `reasoning_effort` field doesn't work with tools in Chat Completions (use Responses API instead); `sot-cli` uses Chat Completions only, so `reasoning_effort` is only relayed for OpenRouter.

`lmstudio`, `openrouter`, `openai`, `ollama`, `nvidia`. New names can be added to `KNOWN_PROVIDERS` in `sot_cli/config/app.py`; the same OpenAI-compatible adapter handles them all.

### Provider capability detection & Context Memory Management

For providers that expose a queryable models endpoint, the adapter performs a one-shot capability detection at startup and populates the `ProviderCapability` dataclass (used to render the banner, provide UI warnings, and gate tool/SoT features per session):

- `lmstudio`: queries `/api/v1/models` (falls back to `/api/v0/models` and `/v1/models`) and reads context length, parameter count, quantization, and modality flags from LM Studio's native response. **It also detects `allocated_context_length` based on the currently loaded instances.**
- `openrouter`: queries `/models` and reads `architecture.input_modalities` and `supported_parameters` to derive tool/vision/PDF/audio/video flags.
- `ollama`: queries `/api/ps` to surface the running model's **allocated context length**, then `/api/show` for parameter count and quantization.
- `nvidia`: queries `/models` for connectivity verification (the endpoint returns a flat list with no architecture metadata), and assumes tool support.
- `openai`: skipped — uses the assumed defaults listed above.
- Unknown OpenAI-compatible names: skipped — minimal `supports_tools=true` default.

**Context UI Warnings:** Based on the detected `allocated_context_length` (or `context_length`), the CLI renders a visual 10-cell bar (`█░░░`) during the Turn Summary. It automatically warns you in yellow when you reach 75% of your context limit, and in red when you reach 90%, suggesting you use `detach_path_from_source` to clear the SoT and avoid token overflow.

### Wizard, `configured` marker, and manual edits

The selector at startup (when no `--provider` flag and no resumed session) considers a provider "configured" iff its `[providers.X]` section in `sot.toml` contains `configured = true`. Single signal — no heuristics on URL or key value.

- **First run** (no `sot.toml`): `_first_run_setup` copies the bundled examples, asks for credentials/URL/model for the chosen provider, and writes `configured = true` at the end.
- **Re-run, picking an unconfigured provider**: a per-provider mini-wizard runs (`_configure_provider_credentials`) — same questions, then writes `configured = true`.
- **Re-run, picking a configured provider**: enters the session directly with whatever the toml currently has.
- **Manual edits**: encouraged. The startup banner renders a `tip:` line in magenta pointing the user to `sot.toml` / `sot.keys.toml`. To force the wizard for a given provider again, just delete its `configured = true` line.

The wizard does not exist for `--provider X` overrides or session resumes (`sot-cli <session_id>`), since both paths assume the user already knows what they want.

## File Discovery Tool (`list_dir`)

`list_dir` is a dual-purpose tool: it can return broad recursive listings and it can act as a filtered search tool.

Current behavior:

- Always recursive.
- Always includes hidden files.
- No built-in result limit by default.
- Returns rich metadata per entry (name, absolute/relative path, type, depth, extension, size, timestamps, hidden/symlink flags, symlink target when available).

Supported filters:

- `kind`: `file`, `directory`, `symlink`, `symlink_file`, `symlink_directory`.
- `extensions`: extension filter list (accepts `.py` and `py` formats).
- `name_contains`: case-insensitive basename substring, supports comma-separated OR keywords.
- `path_contains`: case-insensitive relative/absolute path substring.
- `name_pattern`: glob pattern on basename (`*`, `?`, `[]`).
- `path_pattern`: glob pattern on path (`*`, `?`, `[]`).
- `min_size_bytes` / `max_size_bytes`: numeric size filters.
- `follow_symlinks`: recurse through symlink directories when `true`.

Content search mode:

- `content_contains`: search text inside UTF-8 text files (for example `txt`, `json`, `xml`, `md`, `sql`, `py`, etc.).
- `content_case_sensitive`: make `content_contains` matching case-sensitive.
- `content_max_bytes`: optional max file size for content scanning; larger files are skipped.

When `content_contains` is used, matching entries include content match metadata and the tool returns aggregate content scan statistics (`searched_files`, `matched_files`, and skipped counters).

Typical discovery flow:

- `list_dir` is the primary discovery tool for any file type or use case. Use it first when you need to discover, filter, or narrow down files.
- Once you know the exact path set, switch to `read_files` — it is the single tool for reading file content, used for both one file and batches (pass a `files` array with one or more entries).
- Text files are read in full so the Source of Truth receives the whole authoritative file snapshot.

## Code Search Tool (`search_code`)

`search_code` is a regex-powered content search tool built on [ripgrep](https://github.com/BurntSushi/ripgrep). It complements `list_dir` for cases where you need matching lines with exact line numbers and surrounding context — particularly useful when working with source code.

Supported parameters:

- `pattern` (required): regex pattern to search for.
- `path`: file or directory to search in. Defaults to project root.
- `glob`: file pattern filter (e.g., `"*.py"`, `"*.{ts,tsx}"`).
- `type`: language type filter (e.g., `"py"`, `"js"`, `"rust"`).
- `output_mode`: `"files_with_matches"` (default, returns file paths), `"content"` (matching lines with context), `"count"` (match counts per file).
- `context_before` / `context_after` / `context`: lines of surrounding context in content mode.
- `show_line_numbers`: line numbers in content output (default `true`).
- `case_insensitive`: case-insensitive matching (default `false`).
- `multiline`: patterns spanning multiple lines (default `false`).
- `head_limit`: max result entries (default `200`). Pass `0` for unlimited.
- `offset`: skip first N results for pagination.

When to use `list_dir` vs `search_code`:

- **`list_dir`** is the general-purpose discovery tool. Use it for finding files by name, path, extension, size, timestamps, or broad content matching (`content_contains`). Works for any file type.
- **`search_code`** is specialized for code exploration. Use it when you need regex matching with exact line numbers and surrounding context — finding definitions, usages, imports, or specific patterns across source files.

Typical code exploration flow:

- `search_code` to find where a symbol, function, or pattern is used → `read_files` to pull every relevant file into the SoT → `edit_files` to batch every planned change across all touched files in a single atomic call.

## File Reading Tool (`read_files`)

`read_files` is the single tool for reading file content into the SoT. It accepts a `files` array — pass a one-element array for a single file, or several entries to batch multiple known paths into the same call. There is no separate single-file reader.

Each `files[]` entry takes `path` and optional `pages`/`password` for PDFs. Files are read independently; per-file failures return per-file error entries instead of aborting the whole batch.

Text file behavior:

- Text files are read in full. The complete file is loaded into the SoT and stays available for every following turn until detached.

Non-text behavior:

- PDFs accept `pages` for selecting a page range.
- Images, notebooks, audio, and video are read whole.

## File Editing Tool (`edit_files`)

`edit_files` is the single tool for any text mutation. It accepts a `files` array — each entry carries its own `edits` array applied atomically per file — so one call can edit one file or many. There is no separate single-file editor.

### Targeting modes (per edit, exactly one)

Each edit picks ONE mode by which keys it carries. Mixing modes inside a single edit is rejected.

- **Text mode** — `old_string` (+ optional `new_string`, `before_context`, `after_context`, `replace_all`). Replaces or deletes (`new_string=""`) an exact text span. `replace_all=true` expands to every match after context filtering.
- **Line-range mode** — `start_line` + `end_line` + `new_string`. 1-indexed inclusive. Replaces the line block, or deletes it when `new_string=""`.
- **Insert mode** — `insert_line` + `position` (`"before"` or `"after"`) + `new_string`. Pure zero-width insertion at the line boundary; the anchor line itself is never modified.

### Operations expressed across the modes

- Replace a known string → text mode.
- Delete a string → text mode with `new_string=""`.
- Replace a line block → line-range mode.
- Delete a line block → line-range mode with `new_string=""`.
- Insert new lines → insert mode.
- Append at end of file → insert mode with `insert_line=last_line` and `position="after"`.
- Replace every match → text mode with `replace_all=true`.
- Disambiguate between identical strings → text mode with `before_context`/`after_context` instead of enlarging `old_string`.
- Create a new file → use `write_file` as the canonical tool. `edit_files` supports creation as a single text-mode edit with `old_string=""` and `new_string=<full_content>`, kept for the case where batching the create alongside surgical edits to other files in the same atomic call saves a turn.

### Atomicity

- **Within a file:** all of that file's edits are resolved against the ORIGINAL content first, then spliced in descending offset order. If any edit fails (target not found, line out of range, overlap), nothing in that file is written — the file on disk is untouched.
- **Across files:** per-file independent. One file's failure does not roll back another file's success. The response carries a per-file `results` array with `ok=true/false`; the model can re-emit only the failing entries on the next turn.

### Surgical guarantees per file

- Edits cannot overlap in the original file. Two edits may not share a boundary if either is zero-width (an insert touching another edit) — those must be merged into a single edit with the combined `new_string`.
- Indentation is the model's responsibility — bytes go through verbatim. In whitespace-sensitive languages (Python, YAML, Makefile) the emitted tabs/spaces become the file's tabs/spaces.
- Line endings (LF vs CRLF) are auto-matched to the existing file. Inserts auto-add the file's separator so they never fuse with adjacent lines, and prepend one when appending past an EOF that lacked a trailing newline.
- Curly/typographic quotes in the file are tolerated transparently for matching; replacements respect the file's quote style.
- `old_string=""` is reserved for file creation; it is rejected on existing files.

### SoT update policy (asymmetric on purpose)

- **`operation == "create"`** (file did not exist before this call): the new file is **always** added to the SoT. The model can reason on top of it on the next turn without a separate `read_files` call.
- **`operation == "update"`** on a path **already in the SoT** (read previously, or session-attached): the SoT is refreshed from disk on the next turn so the post-edit content is visible automatically.
- **`operation == "update"`** on a path **not in the SoT** and not session-backed: the file is updated on disk but **not** auto-injected into the SoT. The tool result reports the success; the model's context stays clean. If the model needs that file in the SoT afterwards it must read it explicitly.

This asymmetry optimises for the common case (create-then-work-with-it) while preventing silent context bloat from incidental edits to files the model never tracked.

### History and token economy

The provider-bound payload sanitizer compresses CLOSED turns of `chat_history` before the chat reaches the model. The active turn (the one in flight, including its in-progress tool loop) is never touched. Two effects, applied as a deterministic pure function so prefix-matching prompt caches still hit across rounds and turns:

1. **Reasoning truncation for tool-bearing assistants.** Any assistant message in a closed turn that emitted `tool_calls` has its `reasoning` (and the merged text inside `reasoning_details`) clipped to `[tools].compression_reasoning_trunc_chars` (default 240; 0 disables). The truncated tail is replaced with `...[truncated]`. The reasoning of the FINAL assistant message of a closed turn (the user-facing reply, no `tool_calls`) is left intact — that text is the historical record of what the model said and the user may reference it later.

2. **Pair compression for successful `write_file` / `edit_files`.** When a closed-turn assistant carried EXACTLY ONE `tool_call`, that call's name is in `COMPRESSED_TOOLS` (`write_file`, `edit_files`), and its `tool_response` reports success, the runtime drops both the assistant message and the tool message and inserts a single `user`-role line in their place:

   ```
   SYSTEM MESSAGE: t1 edit_files paths=/abs/x.ts edits=3 sot=tracked_unless_detached result="..." reasoning="..."
   ```

   Multiple compressed pairs in the same round are joined with `|` and numbered `t1`, `t2`, ... Mixed rounds (a compressible tool together with non-compressible tool_calls in the same array) are conservatively left intact — only their reasoning is truncated. Failed mutations are never compressed: the model needs the verbatim error to avoid repeating the same failing call.

The heavy `arguments` body of those past mutations (the full `new_string` blocks for `edit_files`, the full `content` for `write_file`) is therefore permanently dropped from the wire payload — the post-mutation file content is already reflected in the next turn's `=== SOURCE OF TRUTH ===` block. The system prompt teaches the model to read `SYSTEM MESSAGE: ...` lines as runtime logs of its own past actions, not as user instructions.

The tool_call ↔ tool_response single-use pairing invariant is preserved across the compression — the matching tool_response is dropped in the same pass that drops its assistant, so strict providers never see an orphan. See `sot_cli/providers/openai_compat.py::_sanitize_messages_for_provider`.

## Main components

### `sot_cli/query.py`

The core of the orchestrator. Runs the loop against the provider, executes tool calls, maintains the `SoTState`, and rebuilds the SoT after each tool call.

**Payload Assembly & Prompt Caching Optimization:**
To combat "Lost in the Middle" syndrome and maximize API cost savings, the payload is strictly ordered like this:
`[System Prompt] -> [Past Chat History] -> [Ephemeral SoT Block] -> [Latest User Prompt]`

Why this exact order? **Prefix-Matching Prompt Caching.**
Modern APIs (like Anthropic and OpenAI) cache tokens from top to bottom. If a single token changes, the cache breaks for everything below it.

- If we put the dynamic SoT at the _top_, every time a file is edited, the cache would break, and you would pay full price to re-process the entire 100k+ token chat history.
- By putting the static `Chat History` at the top and the dynamic `SoT Block` at the bottom, the API successfully caches the system prompt and your entire conversation history.
- Furthermore, the runtime's closed-turn compression (which converts successful `write_file`/`edit_files` pairs into a single `SYSTEM MESSAGE`) only mutates the _penultimate_ turn. All older turns remain byte-for-byte identical.

This architectural decision guarantees that the historical prefix stays perfectly stable, making long sessions incredibly fast and cheap because the cache only misses on the dynamic tail.

### `sot_cli/providers/openai_compat.py` — Outbound Payload Sanitizer

The last transformation before the chat payload reaches the network. Lives in `_sanitize_messages_for_provider` and runs on every `build_chat_completions_payload` call. The whole pipeline operates on shallow copies of the caller's `chat_history`; the in-memory and on-disk history is never mutated. The transformation is also a deterministic pure function of its inputs, which is what lets prefix-matching prompt caches keep hitting across rounds and turns. Three independent passes:

**1. Active-turn boundary.**
The index of the latest `user` message in the batch marks the beginning of the ACTIVE turn. Everything before it is a CLOSED turn (compression candidate); everything from that index onward is left untouched so the model sees its in-flight tool loop in full detail.

**2. Schema strictness + tool-call ↔ tool-message single-use pairing.**
Streaming interruptions (Ctrl+C mid-generation, network drops, reasoning-budget cuts) and partial responses can leave assistant messages with `content: null` (or whitespace-only) and no surviving `tool_calls`. LM Studio rejects that shape with HTTP 500; OpenAI strict rejects it with 400. The sanitizer drops those husks. `tool`-role messages with `content: null` are coerced to `content: ""` instead of dropped, because dropping would orphan the matching `tool_call`.

OpenAI strict also requires every assistant `tool_call` to be followed by exactly one `tool`-role message carrying the same `tool_call_id` (and vice-versa). Three pathological shapes can land in `chat_history`: a `tool_call` with `arguments: ""` (stream cut before the args delta arrived), a `tool_call` with no matching `tool` response anywhere (assistant emitted but tool never executed), and a `tool_call` whose `id` is duplicated by a later assistant retry. All three trigger HTTP 5xx / 400 from strict providers, with generic error bodies that hide which message was malformed. The sanitizer does a two-pass walk: pass A indexes which `tool_call_id`s have a responding `tool` message in this batch; pass B emits messages in order, single-use-matching each `tool_call` against a pending set so duplicates and orphans are dropped silently. Companion `tool` messages whose call was dropped (or never existed) are dropped too.

**3. SoT-aware compression of CLOSED turns.**
Two effects, applied only to messages strictly before the active-turn boundary:

1.  **Reasoning truncation for tool-bearing assistants.** Any closed-turn assistant message that emitted `tool_calls` has its `reasoning` (and the merged text inside `reasoning_details`) clipped to `[tools].compression_reasoning_trunc_chars` (default 240; 0 disables). The truncated tail is replaced with `...[truncated]`. The reasoning of the FINAL assistant message of a closed turn (the user-facing reply, no `tool_calls`) is NEVER truncated — that text is the historical record of what the model said and the user may reference it later.

2.  **Pair compression for successful `write_file` / `edit_files`.** When a closed-turn assistant carried EXACTLY ONE `tool_call`, that call's name is in `COMPRESSED_TOOLS` (`write_file`, `edit_files`), and its `tool_response` reports success, the runtime drops both the assistant message and the tool message and inserts a single `user`-role line in their place:

    ```
    SYSTEM MESSAGE: t1 edit_files paths=/abs/x.ts edits=3 sot=tracked_unless_detached result="..." reasoning="..."
    ```

    Multiple compressed calls in the same round are joined with `|` and numbered `t1`, `t2`, ... Failed mutations are NEVER compressed: the model needs the verbatim error to avoid repeating the same failing call. Mixed rounds (a compressible tool together with non-compressible tool_calls in the same array) are conservatively left intact — only their reasoning is truncated. The pairing invariant is preserved because the matching tool_response is dropped in the same emit pass that drops its assistant, so strict providers never observe an orphan.

The reasoning of ASSISTANTS IN THE ACTIVE TURN is deliberately LEFT ON in full. OpenRouter and the Anthropic / GPT-5 reasoning class require reasoning to be round-tripped to maintain reasoning continuity within an in-flight turn; stripping it for the active turn would silently degrade those providers. The `[tools].compression_reasoning_trunc_chars` knob therefore only controls how aggressively the runtime clips reasoning of CLOSED turns.

### `sot_cli/source_of_truth.py`

Materializes the persisted session state as `SourceBundle`. It no longer applies configurable caps like file count or size. If a path is attached to the session, the runtime attempts to materialize it.

### `sot_cli/tools/session/delegate.py` (Multi-Agent Orchestration)

Implements the JIT (Just-In-Time) agent pattern.

**The Boss-Worker Dynamic:**

1. **Delegation:** The main agent (Boss) uses `delegate_task` to spawn a sub-agent (Worker) in the background. The sub-agent gets a clean, isolated session (`agent_N`).
2. **Role Separation:** The Boss gets the `SYSTEM_PROMPT` (orchestration, strategy). The Worker gets the `SUB_AGENT_SYSTEM_PROMPT` (strict execution, no delegation allowed).
3. **Invisible IPC (Inter-Process Communication):** The Worker executes tools and outputs its final findings as plain text. The system intercepts this text and writes it to an internal `response.md` file. _The AI models do not know this file exists._
4. **Synchronization:** The Boss uses `wait_task("agent_N")`. The system blocks until the Worker finishes, reads the internal `response.md`, and returns the text directly to the Boss as a tool result.
5. **Terminal Silence:** Sub-agents run completely headless. Their stdout/stderr is redirected to `agent.log` inside their session folder, keeping the main user terminal perfectly clean.

### MCP servers & runtime details

- The runtime can optionally start and manage external MCP servers configured under `mcp.servers` in `sot.toml`. These servers are discovered at runtime and the tools they expose are added to the provider-visible function/schema list.
- MCP-provided tools are namespaced by server (e.g. `myserver__toolname`) so they do not clash with local tool names. MCP tool calls are proxied through the runtime's `MCPManager` and returned as regular tool results.
- Portable local MCP servers can live under the repository `mcps/` folder and be referenced with relative paths from `sot.toml` so the same config works across machines.
- To support delegated child agents, the runtime honors the `SOT_SESSIONS_DIR` environment variable: when set, the runtime points its `SessionStore` to that directory so sub-agents can run inside a parent's `sessions/` folder.
- The runtime caches provider adapters and performs capability detection when needed; this drives tool availability and SoT composition per-provider.
- The streaming provider adapter emits incremental text, tool-call, reasoning/thinking, and usage events when the provider returns them, and the CLI renders them live instead of buffering the whole response first.
- The host-environment context injected into orchestration rules includes explicit local/UTC date-time and weekday fields (including ISO weekday number) so date answers do not rely on model inference.
- **Launch Context Detection:** The runtime automatically detects if the session was launched via `uv`, `conda`, `poetry`, `pipenv`, or `venv`, and injects this metadata into the `CURRENT METADATA` block. This allows the model to correctly format shell commands that require environment activation.

## Current SoT model

### Separation permanent history vs ephemeral SoT

**`chat_history` (permanent):** List of messages that grows between turns. Contains user prompts, assistant responses, and lightweight tool metadata. Never contains file content snapshots.
**SoT block (ephemeral):** Built from disk in each round. Contains the current content of tracked files and media. Injected just before the latest user prompt, and discarded immediately after.

### SoT provenance (Permanent vs. Ephemeral)

The Source of Truth injected into the prompt is a combination of two memory layers:

- **Session-backed (Permanent):** Comes from the persisted `session.json`. These are core files (like preprompts, schemas, or main guidelines) that are refreshed and injected at the start of _every_ turn in that session. Managed via `attach_path_to_source` or the CLI `sot_attach`.
- **Tool-backed (Ephemeral):** Appeared because the model actively read them during the current conversation (e.g., via `read_files` to fix a specific bug). They live in memory to provide immediate context but can be easily discarded using `detach_path_from_source` once the task is done to save tokens.

### Current Metadata Injection (`CURRENT METADATA`)

At the end of each turn, the runtime generates a compact snapshot of the turn's metadata (Token usage, Turn Duration, Launch Context, Agent Statuses). This block is injected as an ephemeral `user` message **between** the SoT block and the next user prompt.
It is never persisted into the `chat_history`. This mechanism provides the model with absolute awareness of its token limits and execution environment without polluting the permanent history.

### Tools compression

The runtime compresses past tool activity between turns to save tokens. Two mechanisms:

**Automatic compression (every turn):**
- `write_file` and `edit_files` calls that succeeded (and were the only tool in that assistant message) are replaced by a single `user`-role line:
  `SYSTEM MESSAGE: write_file path=... sot=tracked_unless_detached result="..." reasoning="..."`.
- Multiple calls in the same round are joined with ` | `.
- The `reasoning` of tool-bearing assistant messages is truncated to `[tools].compression_reasoning_trunc_chars` chars (default 240).
- Failed tool calls are never compressed. The active turn is never compressed.

**Hyper-compression (on demand via `--hypercompress` flag):**
- Reduces chat history size by up to ~70% with zero information loss — tool names, success/failure status, and error descriptions are preserved in a compact summary. The final assistant answer is kept verbatim.
- Collapses whole turn sequences (user prompt → multiple tool calls → final text reply) into:
  `SYSTEM MESSAGE: Assistant requested tools this turn: <tool> (<status>), ...`.
- Each tool is marked `success` or `failed` with a brief error description on failure.
- Creates a timestamped backup before modifying (`request_backup_YYYYMMDD_HHMMSS.json`).
- One-shot operation — run once, then the session continues with normal compression rules.

The model is instructed to treat all `SYSTEM MESSAGE:` lines as runtime logs, not as user instructions or new commands.

### SoT management tools

The model can modify the authoritative working set of the session at any time using these tools:

#### `attach_path_to_source`

Add a file or directory to the session source of truth. Accepts a single path (`path`) or multiple paths in one call (`paths`). Prefer `paths` for batch attach.

Parameters:

- `path` (string): absolute or project-relative path to attach.
- `paths` (array of strings): batch variant — attach several paths in one call. Use this instead of multiple `attach_path_to_source` calls.
- `recursive` (boolean, default `true`): whether to recurse into directories. Applies to every path in the batch.
- `label` (string, optional): human label; only supported with a single path.

Either `path` or `paths` is required.

#### `clean_sot`

Remove ALL tracked files from the session Source of Truth in a single call. Both permanently-attached (session-backed) and ephemerally-read (tool-backed) files are cleared. The files remain on disk — only the tracking is removed. No parameters.

Use this when you need a full context reset instead of detaching files one by one.

#### `detach_path_from_source`

Remove a file or directory from the session source of truth. Accepts a single path (`path`) or multiple paths in one call (`paths`). Prefer `paths` for batch removal.

Parameters:

- `path` (string): absolute path or project-relative path already attached to the session.
- `paths` (array of strings): batch variant — remove several paths in one call. Use this instead of multiple `detach_path_from_source` calls.

Either `path` or `paths` is required.

#### `get_session_state`

Inspect the current session: provider, model, temperature, max output tokens, and the full list of session-backed source entries with their IDs and metadata. Use this before attaching or detaching to confirm the current state. No parameters.

#### `update_session`

Change runtime parameters for future turns in the current session: `title`, `provider`, `model`, `temperature`, `max_output_tokens`. At least one field required.

## Known current limitations

1. Resume/recovery currently depends on reconstructing `chat_history` and SoT state from persisted request/response artifacts.
2. `edit_files` is a surgical text mutator (text-match, line-range, or insert mode), atomic per file and batchable across many files in one call. It is not a regex engine nor an AST editor.
3. `read_files` reads text files in full into the SoT.
4. `search_code` requires [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) to be installed and available in PATH.
5. Archive files (`zip`, `tar`, `gz`, etc.) cannot be read directly. The model receives a format-specific error with the correct `run_command` invocation to list or extract contents.
