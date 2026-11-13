# The Source of Truth (SoT) Method: A Guide to Efficient AI-Powered Development

## 1. The Problem: The Spiraling Cost of Context

When building AI agents for programming, a naive approach involves appending every file read, every code change, and every tool output directly into the conversation history. While simple to implement, this leads to two critical failures:

1.  **Exponential Token Growth:** The conversation payload sent to the API grows uncontrollably. Each time a file is modified, you're not just sending the new version; you're sending the new version _plus_ all previous versions and all intermediate conversational turns. This leads to massive API costs and quickly hits the model's context limit.
2.  **Context Degradation and Hallucination:** As the history gets cluttered with multiple versions of the same file, the AI model can become confused. It might reference a bug that was fixed ten turns ago or apply a change based on an outdated code snippet it found earlier in the conversation.

The **Source of Truth (SoT) Method** is an architectural pattern designed to solve these problems by fundamentally changing how the AI perceives and interacts with the state of a project.

### Line numbers in the SoT block

Every file in the SoT block is displayed with line numbers prefixed to each line:

```
--- FILE: /path/to/file.py (42 lines, 1234 bytes) ---
     1|import argparse
     2|
     3|def main():
     4|    parser = argparse.ArgumentParser()
    ...
--- END: /path/to/file.py ---
```

The format is `{i:>6}|` — 6-digit right-aligned number followed by a pipe.

**Why:** The AI can use `edit_files` with `start_line`/`end_line` directly from the SoT block, eliminating the need for `search_code` to discover line numbers. This saves one tool call per edit when the file is already tracked.

## 2. What is the Source of Truth (SoT) Method?

The core principle of the SoT method is:

> The complete, current state of all relevant files and assets is provided to the model in a single, ephemeral block. This block is rebuilt from the actual source (e.g., the file system) on every single turn and injected dynamically into the payload.

This means the conversation history is kept clean and lightweight, containing only the dialogue and metadata, while the "heavy" content (code, images) is loaded fresh each time, ensuring the model _always_ sees the most up-to-date version and nothing else.

### Core Principles

1.  **The Ephemeral SoT Block:** The SoT is a special `user` message, typically formatted with clear headers. Crucially, **this block is never saved to the permanent conversation history.** Once the model responds, the SoT block used for that turn is discarded.
2.  **Lightweight Tool Results:** In a naive system, the result of a `read_files` tool would be the entire file's content. In the SoT method, the `tool` role message contains only lightweight metadata.
    - **Bad:** `{"role": "tool", "content": "console.log('hello world'); ..."}`
    - **Good (SoT):** `{"role": "tool", "content": "read /path/to/file.js (1 line, 30 bytes) -> added to SoT"}`
3.  **Solving "Lost in the Middle" & Recency Bias:** If the massive SoT block is placed at the very end of the payload, the AI often forgets the user's actual instruction (Lost in the Middle). Furthermore, OpenAI-compatible APIs strictly require `tool` messages to follow `assistant` messages during a tool loop.
    **The Solution:** The SoT block is injected _just before_ the latest user prompt (or the latest ongoing tool-call chain).
    Payload order: `[System Prompt] -> [Past Chat History] -> [SoT Block] -> [Latest User Prompt]`. This ensures the model reads the rules, sees the state of the world, and _finally_ reads the exact instruction it needs to execute.

## 3. The Complete Step-by-Step Example

Let's walk through a development session to see the SoT method in action.

_Initial state: `/path/to/file.js` contains `console.log('hello world');`_

---

### Iteration 1: Reading the Initial File

The user asks the model to read a file. The system executes the tool, reads the file from disk, and constructs the _first_ SoT block for the next round.

**JSON Payload Sent to API (Round 2):**

```json
[
  { "role": "system", "content": "You are the main assistant..." },
  { "role": "user", "content": "review this file /path/to/file.js" },
  { "role": "assistant", "content": null, "tool_calls": [{ "name": "read_files", "args": { "files": [{ "path": "/path/to/file.js" }] } }] },
  { "role": "tool", "content": "batch read 1/1 ok (0 errors) -> SoT updated" },
  {
    "role": "user",
    "content": [
      { "type": "text", "text": "=== SOURCE OF TRUTH ===" },
      { "type": "text", "text": "Files tracked: 1" },
      { "type": "text", "text": "--- FILE: /path/to/file.js (1 lines, 30 bytes) ---\nconsole.log('hello world');\n--- END: /path/to/file.js ---" },
      { "type": "text", "text": "=== END SOURCE OF TRUTH ===" }
    ]
  },
  { "role": "user", "content": "change it to say universe instead of world" }
]
```

_Notice how the SoT is injected right before the user's new instruction._

---

### Iteration 2: Editing the File

The model calls the `edit_files` tool. The system applies the change to the file on disk, saves the assistant's response to the history, and **discards the old SoT block**.

**JSON Payload Sent to API (Round 3):**

```json
[
  { "role": "system", "content": "You are the main assistant..." },
  { "role": "user", "content": "review this file /path/to/file.js" },
  { "role": "assistant", "content": null, "tool_calls": [{ "name": "read_files", "args": { "files": [{ "path": "/path/to/file.js" }] } }] },
  { "role": "tool", "content": "batch read 1/1 ok (0 errors) -> SoT updated" },
  { "role": "user", "content": "change it to say universe instead of world" },
  { "role": "assistant", "content": null, "tool_calls": [{ "name": "edit_files", "args": { "files": [{ "path": "/path/to/file.js", "edits": [{ "old_string": "world", "new_string": "universe" }] }] } }] },
  { "role": "tool", "content": "edit_files: 1/1 ok. - update /path/to/file.js (1 atomic edits, 33 bytes; SoT will be refreshed if file was already tracked)" },
  {
    "role": "user",
    "content": [
      { "type": "text", "text": "=== SOURCE OF TRUTH ===" },
      { "type": "text", "text": "Files tracked: 1" },
      { "type": "text", "text": "--- FILE: /path/to/file.js (1 lines, 33 bytes) ---\nconsole.log('hello universe');\n--- END: /path/to/file.js ---" },
      { "type": "text", "text": "=== END SOURCE OF TRUTH ===" }
    ]
  },
  { "role": "user", "content": "now run it" }
]
```

## 4. Prompt Caching Synergy (The Cost Saver)

The SoT Method is inherently designed to exploit **Prefix-Matching Prompt Caching** (used by Anthropic, OpenAI, etc.). These caches work from top to bottom: if a single token changes, the cache breaks for everything below it.

Because the SoT method places the dynamic, frequently-changing state at the _bottom_ of the payload, the top of the payload remains perfectly static:
`[System Prompt] -> [Past Chat History] -> [Ephemeral SoT Block] -> [Latest User Prompt]`

1. **Stable History:** The `chat_history` only appends new messages. When the runtime compresses past tool calls into lightweight `SYSTEM MESSAGE` logs, it only mutates the _penultimate_ turn. Everything before it remains byte-for-byte identical.
2. **Dynamic Tail:** The `SoT Block` changes every time a file is edited. Because it sits at the end of the payload, it never invalidates the cached history above it.

**Result:** Even if you have 100,000 tokens of conversation history, editing a file only costs you the tokens for the new file state. The API caches the entire history, making long, complex development sessions incredibly fast and up to 90% cheaper.

## 5. Implementation: The Orchestrator Loop

To implement the SoT method, your backend needs an "Orchestrator" loop that follows this logic on every turn:

1.  **Capture:** Receive user input and append it to `chat_history`.
2.  **State Registry:** Check the list of currently active files/images.
3.  **Assemble:** Rebuild the SoT block by reading the current state of those files from the disk.
4.  **Inject:** Splice the payload: `[System] + chat_history[:-1] + [SoT] + chat_history[-1:]`.
5.  **Inference:** Send the payload to the LLM.
6.  **Clean:** Store the assistant's response and tool metadata in the history, but discard the SoT block before the next turn.

### Tool-loop guards and delegated sub-agent limits

The runtime implements additional safety and progress-guard rules around tool execution and delegation:

- After each tool call that mutates session state, the runtime rebuilds the SoT block from disk and refreshes request/session metadata (provider, model, temperature, max output tokens) before the next model turn.
- Delegated sub-agents run with a tighter fail-fast budget than the main session. Current configured limits in the code are: delegated sub-agents max rounds = 8 and delegated repeated-round abort threshold = 2. The main agent uses a higher repeat-round limit (3) before aborting.
- External MCP servers can be configured to provide additional tool schemas; MCP tools are proxied through the runtime and returned as standard tool results.

## 6. Summary of Benefits

- **Massive Token Savings:** The conversation history grows linearly and slowly, containing only dialogue. Heavy assets are loaded only once per turn.
- **Perfect Contextual Awareness:** The model never sees outdated code or assets.
- **Laser Focus:** By injecting the SoT _before_ the latest prompt, the model's attention ends exactly on the instruction it needs to execute.
