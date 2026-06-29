# Roadmap

## Phase 0 - 4

_Completed._ Local persistence, ephemeral Source of Truth (SoT), xAI-compatible adapters, text streaming, basic tool loop, standalone terminal-first assistant, and asynchronous Multi-Agent orchestration (JIT sub-agents).

## Phase 5: Provider Expansion (xAI + Anthropic)

Objective: Add first-class support for `xAI` and `anthropic` providers before moving into advanced browser automation capabilities.

### Deliverables

- Add and validate `xAI` provider configuration and runtime adapter support.
- Add and validate `anthropic` provider configuration and runtime adapter support.
- Ensure provider capability detection works consistently for both new providers.
- Verify feature parity for streaming, tools, and SoT injection behavior.
- Document provider setup examples in `sot.toml` and `sot.keys.toml`.

## Phase 6: The Human-like Browser

Objective: Give the agent the ability to browse the web exactly like a human would, to find documentation, read issues, or interact with web apps.

_Completed._ 15 browser tools powered by browser-use. Supports clean Chromium and real browser profiles (Chrome, Brave, Edge). Tools include navigation, click, type, keyboard, scroll, screenshot, HTML/text extraction, back/forward, and full tab management (new, list, switch). Agent uses screenshots + DOM text for page understanding.

## Phase 7: User-Friendly Web Interface

_Current._

Objective: Make `sot-cli` accessible to less experienced users through an intuitive, visual web-based UI that hides the terminal complexity while retaining full power-user access.

### Deliverables

- Lightweight web interface that runs locally alongside the CLI.
- Session browser and manager: visualize active sessions, chat history, and Source of Truth contents.
- Simple form-based configuration for providers and API keys (no direct TOML editing required).
- Visual file browser and editor for Source of Truth management.
- Real-time streaming output display with tool call visualization.
- Support for both beginners (guided workflows) and power users (direct terminal access).
- Zero backend changes: the web UI calls the same runtime APIs as the CLI, ensuring feature parity.
