from __future__ import annotations

import asyncio
from argparse import ArgumentParser, Namespace
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import signal
import sys
import time

if sys.version_info >= (3, 11):
    import tomllib  # pyright: ignore[reportMissingImports]
else:
    import tomli as tomllib  # pyright: ignore[reportMissingImports]

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.markup import escape  # <-- AÑADIDO PARA EVITAR MARKUP ERRORS

from typing import Any

import os
import platform
import socket
import getpass

from sot_cli.message_builder import detect_launch_context
from sot_cli.prompting import prepare_turn_request
from sot_cli.query import ConversationState, _consolidate_reasoning_details, run_tool_loop
from sot_cli.tools.shell.run_command import try_interrupt_active_foreground
from sot_cli.runtime import AppRuntime, bootstrap_runtime
from sot_cli.hyper_compress import hyper_compress_session
from sot_cli.sot import is_sot_block_content, is_orchestration_rules_content, load_sot_state_from_request_json
from sot_cli.source_of_truth import build_source_bundle, SourceBundle
from sot_cli.providers.base import ProviderCapability
from sot_cli.message_builder import _detect_active_shell, _normalize_arch, _normalize_os_name


console = Console()
error_console = Console(stderr=True)

_COMMAND_NAMES = {
    "prompt",
    "chat",
    "status",
    "command",
    "run_task",
    "sot_attach",
    "sot_show",
    "sot_delete",
}

# Xterm "modified other keys" sequence for Ctrl+Enter. prompt_toolkit maps
# this to Enter/ControlM, but preserves the raw escape sequence in event.data.
_CTRL_ENTER_DATA = {"\x1b[27;5;13~"}


def main(argv: list[str] | None = None) -> int:
    normalized_argv = _normalize_argv_for_default_prompt(argv)
    parser = _build_parser()
    args = parser.parse_args(normalized_argv)

    try:
        return _dispatch(args)
    except Exception as exc:
        # ESCAPAMOS EL ERROR PARA QUE RICH NO CRASHEE SI EL ERROR CONTIENE CORCHETES
        error_console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
        return 1


def _normalize_argv_for_default_prompt(argv: list[str] | None) -> list[str] | None:
    """Insert the implicit `prompt` command when the user omits it."""
    if argv is None:
        raw_args = sys.argv[1:]
        should_return_none = True
    else:
        raw_args = list(argv)
        should_return_none = False

    if not raw_args:
        return None if should_return_none else raw_args

    insert_at = 0
    while insert_at < len(raw_args):
        token = raw_args[insert_at]

        if token in {"-h", "--help", "--list_sessions", "--clean_sot"}:
            return None if should_return_none else raw_args

        if token == "--config":
            insert_at += 2
            continue

        if token.startswith("--config="):
            insert_at += 1
            continue

        if token in _COMMAND_NAMES:
            return None if should_return_none else raw_args

        normalized = raw_args[:insert_at] + ["prompt"] + raw_args[insert_at:]
        return normalized if not should_return_none else normalized

    return None if should_return_none else raw_args


def _submit_shortcut_help_text() -> str:
    if sys.platform == "darwin":
        return "Use Alt+Enter to send. If it doesn't work, use Esc then Enter."
    if sys.platform.startswith("win"):
        return "Use Ctrl+Enter to send. Fallback: Esc then Enter."
    return "Use Ctrl+Enter to send if your terminal supports it; otherwise Esc then Enter."


def _format_capability_line(cap: ProviderCapability) -> tuple[str, str]:
    stats: list[str] = []
    
    if cap.allocated_context_length or cap.context_length:
        if cap.allocated_context_length and cap.context_length and cap.allocated_context_length != cap.context_length:
            val = f"{_format_token_count(cap.allocated_context_length)} alloc / {_format_token_count(cap.context_length)} max"
        elif cap.allocated_context_length:
            val = f"{_format_token_count(cap.allocated_context_length)}"
        else:
            val = f"{_format_token_count(cap.context_length)}"
        stats.append(f"ctx={val}")
        
    if cap.parameter_count:
        stats.append(f"params={cap.parameter_count}")
    if cap.quantization:
        stats.append(f"quant={cap.quantization}")

    flags: list[str] = []
    if cap.supports_tools: flags.append("tools")
    if cap.supports_images: flags.append("vision")
    if cap.supports_pdfs: flags.append("pdf")
    if cap.supports_audio: flags.append("audio")
    if cap.supports_video: flags.append("video")

    stats_line = " | ".join(stats) if stats else ""
    caps_line = f"capabilities={','.join(flags)}" if flags else ""
    
    return stats_line, caps_line


def _format_token_count(n: int) -> str:
    if n >= 1_000_000:
        if n % 1_000_000 == 0:
            return f"{n // 1_000_000}M"
        if n % (1024 * 1024) == 0:
            return f"{n // (1024 * 1024)}M"
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        if n % 1000 == 0:
            return f"{n // 1000}k"
        if n % 1024 == 0:
            return f"{n // 1024}k"
        return f"{n // 1000}k"
    return str(n)


def _load_chat_history_from_request_jsons(session_dir: Path) -> list[dict[str, Any]] | None:
    request_path = session_dir / "request.json"
    chunks_path = session_dir / "response-chunks.json"

    if not request_path.exists():
        return None

    try:
        request_data = json.loads(request_path.read_text(encoding="utf-8"))
        messages = request_data.get("payload", {}).get("messages", [])
    except (json.JSONDecodeError, KeyError, OSError):
        return None

    if not messages:
        return None

    chat_messages: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            continue
        content = msg.get("content", "")
        if role == "user" and is_sot_block_content(content):
            continue
        if role == "user" and is_orchestration_rules_content(content):
            continue
        if role == "user" and isinstance(content, str) and content.startswith("=== CURRENT METADATA ==="):
            continue
        if role == "assistant" and isinstance(msg.get("reasoning_details"), list):
            cleaned = dict(msg)
            cleaned["reasoning_details"] = _consolidate_reasoning_details(msg["reasoning_details"])
            chat_messages.append(cleaned)
            continue
        chat_messages.append(msg)

    if chunks_path.exists():
        try:
            chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
            assistant_msg = _reconstruct_assistant_from_chunks(chunks)
            if assistant_msg:
                chat_messages.append(assistant_msg)
        except (json.JSONDecodeError, OSError):
            pass

    return chat_messages if chat_messages else None


def _reconstruct_assistant_from_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any] | None:
    text_parts: list[str] = []
    tool_state: dict[int, dict[str, Any]] = {}

    for chunk in chunks:
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or choice.get("message") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
            for tc in delta.get("tool_calls", []):
                index = int(tc.get("index", 0))
                entry = tool_state.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if tc.get("id"):
                    entry["id"] = tc["id"]
                func = tc.get("function", {})
                if func.get("name"):
                    entry["function"]["name"] += func["name"]
                if func.get("arguments"):
                    entry["function"]["arguments"] += func["arguments"]

    text = "".join(text_parts)
    tool_calls = [tool_state[i] for i in sorted(tool_state)] if tool_state else []

    if not text and not tool_calls:
        return None

    msg: dict[str, Any] = {"role": "assistant", "content": text if text else None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _replay_conversation(history: list[dict[str, Any]]) -> None:
    console.print("[dim]─── session history ───[/dim]")
    for msg in history:
        role = msg.get("role", "")
        if role == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                console.print(f"[bold cyan]you>[/bold cyan] {escape(content)}")
        elif role == "assistant":
            text = msg.get("content") or ""
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                console.print(f"[blue]assistant>[/blue] [dim]called {escape(', '.join(names))}[/dim]")
            if text:
                console.print(f"[blue]assistant>[/blue] {escape(text)}")
        elif role == "tool":
            content = msg.get("content", "")
            console.print(f"[dim]tool> {escape(content)}[/dim]")
    console.print("[dim]─── end of history ───[/dim]\n")


def _save_last_turn_metadata(
    session_dir: Path,
    snapshot: dict[str, Any],
    render_extras: dict[str, Any] | None = None,
) -> None:
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        target = session_dir / "turn_metadata.json"
        wrapper = {"snapshot": snapshot, "render": render_extras or {}}
        target.write_text(
            json.dumps(wrapper, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError):
        return


def _load_last_turn_metadata(session_dir: Path) -> dict[str, Any] | None:
    target = session_dir / "turn_metadata.json"
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if "snapshot" in data and isinstance(data["snapshot"], dict):
                    return {
                        "snapshot": data["snapshot"],
                        "render": data.get("render") if isinstance(data.get("render"), dict) else {},
                    }
                if data:
                    return {"snapshot": data, "render": {}}
        except (json.JSONDecodeError, OSError):
            pass

    request_path = session_dir / "request.json"
    if not request_path.exists():
        return None
    try:
        request_data = json.loads(request_path.read_text(encoding="utf-8"))
        messages = request_data.get("payload", {}).get("messages", [])
    except (json.JSONDecodeError, OSError, AttributeError):
        return None
    if not isinstance(messages, list):
        return None

    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.startswith("=== CURRENT METADATA ==="):
            continue
        body = content
        for marker in ("=== CURRENT METADATA ===", "=== END CURRENT METADATA ==="):
            body = body.replace(marker, "")
        body = body.strip()
        snap: dict[str, Any] = {}
        for pair in body.split(";"):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            key, _, val = pair.partition(":")
            snap[key.strip()] = val.strip()
        if snap:
            return {"snapshot": snap, "render": {}}

    return None


def _render_resumed_summary(
    snapshot: dict[str, Any],
    render_extras: dict[str, Any],
    session_id: str,
) -> None:
    if not snapshot and not render_extras:
        return

    table = Table(title="Last Turn Summary & Usage (restored)")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Session ID", escape(str(snapshot.get("Session ID", session_id))))

    main_tokens = snapshot.get("Main Agent Tokens")
    if main_tokens not in (None, ""):
        table.add_row("Main Agent Tokens", escape(str(main_tokens)))
    delegated = snapshot.get("Sub-Agents Tokens")
    if delegated:
        table.add_row("Sub-Agents Tokens", escape(str(delegated)))
    total = snapshot.get("Total Tokens")
    if total not in (None, ""):
        table.add_row("Total Tokens", escape(str(total)), style="bold cyan")
    cost = snapshot.get("Total Cost")
    if cost:
        table.add_row("Total Cost", escape(str(cost)), style="bold green")

    ctx_pct = render_extras.get("ctx_pct")
    ctx_prompt = render_extras.get("ctx_prompt")
    ctx_max = render_extras.get("ctx_max")
    ctx_label = render_extras.get("ctx_label") or "Context Limit"
    if (
        isinstance(ctx_pct, (int, float))
        and isinstance(ctx_max, (int, float))
        and ctx_max > 0
    ):
        pct = int(ctx_pct)
        filled = int((pct / 100) * 10)
        bar = "█" * filled + "░" * (10 - filled)
        color = "red" if pct > 90 else "yellow" if pct > 75 else "green"
        prompt_n = int(ctx_prompt) if isinstance(ctx_prompt, (int, float)) else 0
        table.add_row(
            escape(ctx_label),
            f"[{color}]{bar} {pct}% ({prompt_n}/{int(ctx_max)})[/{color}]",
        )
        if pct >= 90:
            table.add_row(
                "Warning",
                "[bold red]⚠️ Context almost full! Ask the model to remove some not used SoT files (If Any) [/bold red]",
            )
        elif pct >= 75:
            table.add_row(
                "Warning",
                "[bold yellow]⚠️ Context is getting full. Consider detaching unused files.[/bold yellow]",
            )
    elif snapshot.get("Context"):
        table.add_row("Context", escape(str(snapshot["Context"])))

    sot_files = render_extras.get("sot_files") or []
    if isinstance(sot_files, list) and sot_files:
        try:
            table.add_section()
        except Exception:
            pass
        table.add_row(
            "SoT Tracked Files",
            "Always updated in real time => " + str(len(sot_files)),
            style="bold magenta",
        )
        for fpath in sorted(str(p) for p in sot_files):
            fname = Path(fpath).name
            table.add_row(f"  📄 {escape(fname)}", "[dim]in context[/dim]")
    elif snapshot.get("SoT Tracked Files"):
        try:
            table.add_section()
        except Exception:
            pass
        table.add_row(
            "SoT Tracked Files",
            escape(str(snapshot["SoT Tracked Files"])),
            style="bold magenta",
        )

    agents = render_extras.get("agents") or []
    if isinstance(agents, list) and agents:
        try:
            table.add_section()
        except Exception:
            pass
        table.add_row("Agents Used", str(len(agents)))
        for entry in agents:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                name, status = entry[0], entry[1]
            elif isinstance(entry, dict):
                name = entry.get("name", "?")
                status = entry.get("status", "UNKNOWN")
            else:
                continue
            color = "green" if str(status).upper() == "SUCCESS" else "red"
            table.add_row(escape(f"  {name}"), f"[{color}]{escape(str(status))}[/{color}]")
    elif snapshot.get("Agents Used"):
        try:
            table.add_section()
        except Exception:
            pass
        table.add_row("Agents Used", escape(str(snapshot["Agents Used"])))

    try:
        table.add_section()
    except Exception:
        pass
    if snapshot.get("Timestamp"):
        table.add_row("Timestamp", escape(str(snapshot["Timestamp"])))
    if snapshot.get("Turn Duration"):
        table.add_row("Turn Duration", escape(str(snapshot["Turn Duration"])), style="bold yellow")

    console.print(table)


# ── First-run setup & provider selector ─────────────────────────────────


def _detect_first_run_root() -> Path | None:
    start = Path.cwd().resolve()
    for directory in (start, *start.parents):
        if (directory / "sot.example.toml").exists():
            sot_toml = directory / "sot.toml"
            keys_toml = directory / "sot.keys.toml"
            if not sot_toml.exists() or not keys_toml.exists():
                return directory
            return None
    return None


def _read_provider_names_from_toml(toml_path: Path) -> list[str]:
    try:
        with toml_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    providers = data.get("providers", {})
    if not isinstance(providers, dict):
        return []
    return [name for name, value in providers.items() if isinstance(value, dict)]


def _read_toml_string(toml_path: Path, section_path: list[str], field: str, default: str = "") -> str:
    try:
        with toml_path.open("rb") as handle:
            data: Any = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return default
    cur: Any = data
    for key in section_path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key, {})
    if not isinstance(cur, dict):
        return default
    value = cur.get(field, default)
    return str(value) if value is not None else default


def _extract_section_header(line: str) -> str | None:
    stripped = line.lstrip()
    if not stripped.startswith("["):
        return None
    end = stripped.find("]")
    if end == -1:
        return None
    header = stripped[: end + 1]
    rest = stripped[end + 1 :].lstrip()
    if rest and not rest.startswith("#"):
        return None
    return header


def _update_toml_string_field(toml_path: Path, section_header: str, field: str, new_value: str) -> bool:
    try:
        text = toml_path.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.split("\n")
    in_target = False
    pattern = re.compile(rf'^(\s*){re.escape(field)}\s*=\s*"[^"]*"(.*)$')
    for i, line in enumerate(lines):
        header = _extract_section_header(line)
        if header is not None:
            in_target = header == section_header
            continue
        if in_target:
            match = pattern.match(line)
            if match:
                indent, trailing = match.group(1), match.group(2)
                lines[i] = f'{indent}{field} = "{new_value}"{trailing}'
                toml_path.write_text("\n".join(lines), encoding="utf-8")
                return True
    return False


def _ask(prompt_text: str) -> str:
    try:
        return input(prompt_text)
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]aborted[/dim]")
        sys.exit(0)


def _select_provider_interactive(providers: list[str], current_default: str | None = None) -> str:
    if not providers:
        error_console.print("[red]No providers found in sot.toml.[/red]")
        sys.exit(1)

    console.print("\n[bold]Select a provider:[/bold]")
    for i, name in enumerate(providers, 1):
        marker = "  [yellow](default)[/yellow]" if name == current_default else ""
        hint = "  [dim](or any OpenAI-compatible API)[/dim]" if name == "openai" else ""
        console.print(f"  {i}. {name}{hint}{marker}")

    while True:
        prompt_suffix = f" [Enter for {current_default}]" if current_default in providers else ""
        choice = _ask(f"Enter number or name{prompt_suffix}: ").strip()
        if not choice and current_default in providers:
            return current_default
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(providers):
                return providers[idx]
        elif choice in providers:
            return choice
        console.print(f"[red]Invalid choice: {escape(choice)}[/red]")


def _first_run_setup() -> str:
    root = _detect_first_run_root()
    if root is None:
        error_console.print(
            "[red]Could not locate sot.example.toml.[/red] "
            "Run sot-cli from inside the project tree, or pass --config <path>."
        )
        sys.exit(1)

    sot_toml = root / "sot.toml"
    sot_keys_toml = root / "sot.keys.toml"
    sot_example = root / "sot.example.toml"
    sot_keys_example = root / "sot.keys.example.toml"

    sot_logo = (
        "[bold cyan]"
        " ▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄      ▄▄▄▄▄▄▄  ▄        ▄▄▄▄▄▄▄ \n"
        "▐░░░░░░░▌▐░░░░░░░▌▐░░░░░░░▌    ▐░░░░░░░▌▐░▌      ▐░░░░░░░▌\n"
        "▐░█▀▀▀▀▀ ▐░█▀▀▀█░▌ ▀▀█░█▀▀     ▐░█▀▀▀▀▀ ▐░▌       ▀▀█░█▀▀ \n"
        "▐░█▄▄▄▄▄ ▐░▌   ▐░▌   ▐░▌ ▄▄▄▄▄ ▐░▌      ▐░▌         ▐░▌   \n"
        "▐░░░░░░░▌▐░▌   ▐░▌   ▐░▌▐░░░░░▌▐░▌      ▐░▌         ▐░▌   \n"
        " ▀▀▀▀▀█░▌▐░▌   ▐░▌   ▐░▌ ▀▀▀▀▀ ▐░▌      ▐░▌         ▐░▌   \n"
        " ▄▄▄▄▄█░▌▐░█▄▄▄█░▌   ▐░▌       ▐░█▄▄▄▄▄ ▐░█▄▄▄▄▄  ▄▄█░█▄▄ \n"
        "▐░░░░░░░▌▐░░░░░░░▌   ▐░▌       ▐░░░░░░░▌▐░░░░░░░▌▐░░░░░░░▌\n"
        " ▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀     ▀         ▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀ \n"
        "[/bold cyan]"
    )
    console.print()
    console.print(sot_logo)
    console.print()
    console.print(
        "[bold magenta]✨ Welcome to sot-cli — and thank you for installing it.[/bold magenta]"
    )
    console.print()
    console.print(
        "[bold cyan]A pragmatic, limitless, multi-provider terminal AI agent[/bold cyan] "
        "built around the [bold]Source of Truth (SoT) Method[/bold]."
    )
    console.print(
        "[bold yellow]No bloat. No babysitter. No token waste.[/bold yellow] "
        "Just raw model power, ephemeral file context, async sub-agents,"
    )
    console.print(
        "and unrestricted local tools — all wired straight into your terminal."
    )
    console.print()
    console.print(
        "[bold]This is your first launch.[/bold] "
        "Let's connect you to a provider so you can start chatting in seconds."
    )
    console.print(
        "[yellow]Everything is editable later: sot.toml for models and base URLs, "
        "sot.keys.toml for API keys.[/yellow]"
    )
    console.print()

    source_toml = sot_toml if sot_toml.exists() else sot_example
    source_keys = sot_keys_toml if sot_keys_toml.exists() else sot_keys_example
    if not source_toml.exists() or not source_keys.exists():
        error_console.print(
            f"[red]Missing template files in {escape(str(root))}.[/red] "
            "Expected sot.example.toml and sot.keys.example.toml."
        )
        sys.exit(1)

    providers = _read_provider_names_from_toml(source_toml)
    selected = _select_provider_interactive(providers)
    is_local = selected in {"lmstudio", "ollama"}
    key_optional = selected in _OPTIONAL_KEY_PROVIDERS

    if key_optional:
        key_input = _ask(
            f"\nAPI key for {selected} (optional, press Enter to leave empty): "
        ).strip()
    else:
        while True:
            key_input = _ask(f"\nAPI key for {selected}: ").strip()
            if key_input:
                break
            console.print(f"[red]An API key is required for {escape(selected)}.[/red]")

    new_url: str | None = None
    if is_local:
        new_url = _ask_local_url(selected)
    elif selected == "openai":
        new_url = _ask_openai_url()

    new_model: str | None = None
    if selected in _MODEL_CONFIGURABLE_PROVIDERS:
        current_model = _read_toml_string(source_toml, ["providers", selected], "model")
        new_model = _ask_model(selected, current_model)

    if not sot_toml.exists():
        console.print(
            f"[yellow]Creating {escape(sot_toml.name)} from {escape(sot_example.name)}...[/yellow]"
        )
        shutil.copy2(sot_example, sot_toml)
    if not sot_keys_toml.exists():
        console.print(
            f"[yellow]Creating {escape(sot_keys_toml.name)} from {escape(sot_keys_example.name)}...[/yellow]"
        )
        shutil.copy2(sot_keys_example, sot_keys_toml)

    _update_toml_string_field(sot_keys_toml, f"[providers.{selected}]", "api_key", key_input)
    if new_url is not None:
        current_url = _read_toml_string(sot_toml, ["providers", selected], "base_url")
        if new_url != current_url:
            _update_toml_string_field(sot_toml, f"[providers.{selected}]", "base_url", new_url)
    if new_model:
        current_model_in_toml = _read_toml_string(sot_toml, ["providers", selected], "model")
        if new_model != current_model_in_toml:
            _update_toml_string_field(sot_toml, f"[providers.{selected}]", "model", new_model)
    _update_toml_string_field(sot_toml, "[runtime]", "primary_provider", selected)
    _set_toml_bool_field(sot_toml, f"[providers.{selected}]", "configured", True)

    final_url = _read_toml_string(sot_toml, ["providers", selected], "base_url")
    final_model = _read_toml_string(sot_toml, ["providers", selected], "model")
    console.print("\n[bold green]Setup complete.[/bold green]\n")
    console.print(
        f"  [bold]provider[/bold]  [bold yellow]{escape(selected.upper())}[/bold yellow]"
    )
    console.print(
        f"  [bold]route[/bold]     [yellow]{escape(final_url)}[/yellow]"
    )
    if final_model:
        console.print(
            f"  [bold]model[/bold]     [yellow]{escape(final_model)}[/yellow]"
        )
    console.print()
    console.print(
        "To change models or base URLs, edit [bold yellow]sot.toml[/bold yellow]."
    )
    console.print(
        "API keys live in [bold yellow]sot.keys.toml[/bold yellow]."
    )
    console.print(
        "For the full command and tool reference, read [bold yellow]ARCHITECTURE.md[/bold yellow]."
    )
    console.print()

    return selected


_LOCAL_DEFAULT_PORTS: dict[str, str] = {
    "lmstudio": "1234",
    "ollama": "11434",
}

_OPTIONAL_KEY_PROVIDERS: set[str] = {"lmstudio", "ollama", "openai"}
_MODEL_CONFIGURABLE_PROVIDERS: set[str] = {"openrouter", "nvidia", "openai"}
_OPENAI_DEFAULT_URL = "https://api.openai.com/v1"


def _ask_model(provider: str, current_default: str) -> str:
    console.print(f"\n[bold]Model for {escape(provider)}[/bold]")
    if current_default:
        console.print(f"  default: [yellow]{escape(current_default)}[/yellow]")
        entered = _ask(
            "Press Enter to keep the default, or type a model name: "
        ).strip()
        return entered if entered else current_default
    return _ask("Type a model name: ").strip()


def _ask_openai_url() -> str:
    console.print(
        "\n[bold]Base URL for openai (or any OpenAI-compatible API)[/bold]"
    )
    console.print(f"  default: [yellow]{escape(_OPENAI_DEFAULT_URL)}[/yellow]")
    while True:
        entered = _ask(
            "Press Enter to keep the default, or paste a custom URL: "
        ).strip()
        if not entered:
            return _OPENAI_DEFAULT_URL
        if not (entered.startswith("http://") or entered.startswith("https://")):
            console.print("[red]URL must start with http:// or https://.[/red]")
            continue
        url = entered.rstrip("/")
        if not url.endswith("/v1"):
            url = f"{url}/v1"
        return url


def _ask_local_url(provider: str) -> str:
    console.print(f"\n[bold]Where is the {escape(provider)} server running?[/bold]")
    console.print("  1. localhost  [yellow](default)[/yellow]")
    console.print("  2. Custom IP / hostname")
    console.print("  3. Custom URL (tunnel, reverse proxy, etc.)")

    host: str | None = None
    while host is None:
        choice = _ask("Enter number (leave blank for localhost): ").strip()
        if not choice or choice == "1" or choice.lower() == "localhost":
            host = "localhost"
            break
        if choice == "2":
            entered = _ask("Enter the IP address or hostname (e.g. 192.168.1.10): ").strip()
            if entered:
                host = entered
                break
            console.print("[red]Host cannot be empty.[/red]")
            continue
        if choice == "3":
            url = _ask(
                "Enter the full URL (e.g. https://lms.example.com or https://lms.example.com/v1): "
            ).strip()
            if not url:
                console.print("[red]URL cannot be empty.[/red]")
                continue
            if not (url.startswith("http://") or url.startswith("https://")):
                console.print("[red]URL must start with http:// or https://.[/red]")
                continue
            url = url.rstrip("/")
            if not url.endswith("/v1"):
                url = f"{url}/v1"
            return url
        console.print(f"[red]Invalid choice: {escape(choice)}[/red]")

    port = _ask_local_port(provider)
    return f"http://{host}:{port}/v1"


def _ask_local_port(provider: str) -> str:
    default_port = _LOCAL_DEFAULT_PORTS.get(provider, "")
    suffix = f" (leave blank for {default_port})" if default_port else ""
    while True:
        port_input = _ask(f"Port{suffix}: ").strip()
        if not port_input and default_port:
            return default_port
        if port_input.isdigit() and 0 < int(port_input) < 65536:
            return port_input
        console.print(f"[red]Invalid port: {escape(port_input)}[/red]")


# ── TOML field insertion helper ─────────────────────────────────────────


def _set_toml_string_field(
    toml_path: Path, section_header: str, field: str, new_value: str
) -> bool:
    if _update_toml_string_field(toml_path, section_header, field, new_value):
        return True

    try:
        text = toml_path.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.split("\n")
    section_idx: int | None = None
    for i, line in enumerate(lines):
        if _extract_section_header(line) == section_header:
            section_idx = i
            break
    if section_idx is None:
        return False

    insert_at = len(lines)
    for j in range(section_idx + 1, len(lines)):
        if _extract_section_header(lines[j]) is not None:
            insert_at = j
            break
    while insert_at > section_idx + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1

    lines.insert(insert_at, f'{field} = "{new_value}"')
    toml_path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _set_toml_bool_field(
    toml_path: Path, section_header: str, field: str, value: bool
) -> bool:
    try:
        text = toml_path.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.split("\n")
    literal = "true" if value else "false"

    in_target = False
    pattern = re.compile(rf'^(\s*){re.escape(field)}\s*=\s*(?:true|false)(.*)$')
    for i, line in enumerate(lines):
        header = _extract_section_header(line)
        if header is not None:
            in_target = header == section_header
            continue
        if in_target:
            match = pattern.match(line)
            if match:
                indent, trailing = match.group(1), match.group(2)
                lines[i] = f"{indent}{field} = {literal}{trailing}"
                toml_path.write_text("\n".join(lines), encoding="utf-8")
                return True

    section_idx: int | None = None
    for i, line in enumerate(lines):
        if _extract_section_header(line) == section_header:
            section_idx = i
            break
    if section_idx is None:
        return False

    insert_at = len(lines)
    for j in range(section_idx + 1, len(lines)):
        if _extract_section_header(lines[j]) is not None:
            insert_at = j
            break
    while insert_at > section_idx + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1

    lines.insert(insert_at, f"{field} = {literal}")
    toml_path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _read_toml_bool(
    toml_path: Path, section_path: list[str], field: str, default: bool = False
) -> bool:
    try:
        with toml_path.open("rb") as handle:
            data: Any = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return default
    cur: Any = data
    for key in section_path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key, {})
    if not isinstance(cur, dict):
        return default
    value = cur.get(field, default)
    return bool(value) if isinstance(value, bool) else default


# ── Per-provider re-config (selector path) ──────────────────────────────


def _is_provider_configured(provider: str, sot_toml: Path) -> bool:
    return _read_toml_bool(sot_toml, ["providers", provider], "configured", default=False)


def _configure_provider_credentials(
    provider: str, sot_toml: Path, sot_keys_toml: Path
) -> None:
    is_local = provider in {"lmstudio", "ollama"}
    key_optional = provider in _OPTIONAL_KEY_PROVIDERS

    console.print()
    console.print(
        f"[bold yellow]The {escape(provider)} provider has not been configured yet — "
        f"let's set it up.[/bold yellow]"
    )

    if key_optional:
        key_input = _ask(
            f"\nAPI key for {provider} (optional, press Enter to leave empty): "
        ).strip()
    else:
        while True:
            key_input = _ask(f"\nAPI key for {provider}: ").strip()
            if key_input:
                break
            console.print(f"[red]An API key is required for {escape(provider)}.[/red]")

    _update_toml_string_field(sot_keys_toml, f"[providers.{provider}]", "api_key", key_input)

    if is_local:
        new_url = _ask_local_url(provider)
        current_url = _read_toml_string(sot_toml, ["providers", provider], "base_url")
        if new_url != current_url:
            _update_toml_string_field(sot_toml, f"[providers.{provider}]", "base_url", new_url)
    elif provider == "openai":
        new_url = _ask_openai_url()
        current_url = _read_toml_string(sot_toml, ["providers", provider], "base_url")
        if new_url != current_url:
            _update_toml_string_field(sot_toml, f"[providers.{provider}]", "base_url", new_url)

    if provider in _MODEL_CONFIGURABLE_PROVIDERS:
        current_model = _read_toml_string(sot_toml, ["providers", provider], "model")
        new_model = _ask_model(provider, current_model)
        if new_model and new_model != current_model:
            _update_toml_string_field(sot_toml, f"[providers.{provider}]", "model", new_model)

    _set_toml_bool_field(sot_toml, f"[providers.{provider}]", "configured", True)

    final_url = _read_toml_string(sot_toml, ["providers", provider], "base_url")
    final_model = _read_toml_string(sot_toml, ["providers", provider], "model")
    console.print(f"\n[bold green]{escape(provider.upper())} configured.[/bold green]\n")
    console.print(f"  [bold]provider[/bold]  [bold yellow]{escape(provider.upper())}[/bold yellow]")
    console.print(f"  [bold]route[/bold]     [yellow]{escape(final_url)}[/yellow]")
    if final_model:
        console.print(f"  [bold]model[/bold]     [yellow]{escape(final_model)}[/yellow]")
    console.print()


# ── Dispatch ─────────────────────────────────────────────────────────────


def _dispatch(args: Namespace) -> int:
    if args.list_sessions:
        _list_sessions(getattr(args, 'sessions_dir', None))
        return 0

    if args.clean_sot:
        _clean_sot_session(args.clean_sot, getattr(args, 'sessions_dir', None))
        return 0

    if not args.config:
        first_run_root = _detect_first_run_root()
        if first_run_root is not None:
            if not sys.stdin.isatty():
                error_console.print(
                    "[red]Config files (sot.toml / sot.keys.toml) are missing.[/red] "
                    "Run sot-cli interactively to perform first-run setup."
                )
                return 1
            chosen = _first_run_setup()
            args.provider = chosen

    runtime = bootstrap_runtime(args.config)

    if (
        args.command in {None, "prompt", "chat"}
        and not getattr(args, "provider", None)
        and not getattr(args, "session_id", None)
        and sys.stdin.isatty()
    ):
        available = [
            name for name, cfg in runtime.config.providers.items() if cfg.enabled
        ]
        default_provider = runtime.config.runtime.primary_provider
        if available:
            chosen = _select_provider_interactive(available, current_default=default_provider)
            args.provider = chosen

            sot_toml = runtime.paths.config_file
            sot_keys_toml = sot_toml.with_name("sot.keys.toml")
            if not _is_provider_configured(chosen, sot_toml):
                _configure_provider_credentials(chosen, sot_toml, sot_keys_toml)
                runtime = bootstrap_runtime(args.config)

    async def _run_with_cleanup():
        try:
            await runtime.mcp.start()
            if args.command in {None, "prompt", "chat"}:
                return await _run_prompt(
                    runtime, getattr(args, "session_id", None), getattr(args, "title", None),
                    getattr(args, "provider", None), getattr(args, "model", None), getattr(args, "no_tools", False),
                    subagent_model_override=getattr(args, "subagent_model", None),
                    hypercompress=getattr(args, "hypercompress", False),
                )
            if args.command == "command":
                return await _run_command_turn(
                    runtime, args.session_id, args.prompt, args.provider,
                    args.model, args.no_tools, getattr(args, "disable_delegation", False)
                )
            if args.command == "run_task":
                return await _run_task(runtime, args.agent_id, args.prompt)
        finally:
            try:
                await runtime.mcp.close()
            except Exception:
                pass

    if args.command in {None, "prompt", "chat", "command", "run_task"}:
        return asyncio.run(_run_with_cleanup())

    if args.command == "status":
        _print_status(runtime)
        return 0


    if args.command == "sot_attach":
        record = runtime.sessions.attach_path(
            session_id=args.session_id, target_path=args.target,
            label=args.label, recursive=not args.non_recursive,
        )
        console.print(f"[green]Attached to SoT in session {escape(record.id)}[/green]")
        return 0

    if args.command == "sot_show":
        _print_sot(runtime, args.session_id)
        return 0

    if args.command == "sot_delete":
        ref = args.ref
        try:
            record, removed = runtime.sessions.remove_source_entry(args.session_id, entry_id=ref)
            console.print(f"[green]Removed '{escape(removed.label)}' ({escape(removed.id)}) from SoT.[/green]")
        except FileNotFoundError:
            try:
                record, removed = runtime.sessions.remove_source_entry(args.session_id, path=ref)
                console.print(f"[green]Removed '{escape(removed.label)}' ({escape(removed.id)}) from SoT.[/green]")
            except FileNotFoundError:
                console.print(f"[red]Error: No entry with ID or path '{escape(ref)}' in session {escape(args.session_id)}.[/red]")
                return 1
        return 0

    raise ValueError(f"Unknown command: {args.command}")


# ── Parser ───────────────────────────────────────────────────────────────


def _build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="sot-cli")
    parser.add_argument("--config", default=None, help="Path to sot.toml")
    parser.add_argument("--list_sessions", action="store_true", help="List all sessions as JSON")
    parser.add_argument("--clean_sot", default=None, metavar="SESSION_ID", help="Remove all SoT tracked files from a session")
    subparsers = parser.add_subparsers(dest="command")

    _PROVIDER_CHOICES = ["lmstudio", "openrouter", "openai", "xai", "ollama", "nvidia"]

    prompt = subparsers.add_parser("prompt", help="Start or resume an interactive session")
    prompt.add_argument("session_id", nargs="?", default=None, help="Resume an existing session by ID")
    prompt.add_argument("--title", default=None, help="Title for the new session")
    prompt.add_argument("--provider", choices=_PROVIDER_CHOICES, default=None)
    prompt.add_argument("--model", default=None)
    prompt.add_argument("--no-tools", action="store_true", help="Plain chat without tool loop")
    prompt.add_argument("--hypercompress", action="store_true", help="Run hyper-compression on session history before starting")

    chat = subparsers.add_parser("chat", help="Alias for prompt")
    chat.add_argument("session_id", nargs="?", default=None)
    chat.add_argument("--title", default=None)
    chat.add_argument("--provider", choices=_PROVIDER_CHOICES, default=None)
    chat.add_argument("--model", default=None)
    chat.add_argument("--no-tools", action="store_true")
    chat.add_argument("--hypercompress", action="store_true")

    cmd = subparsers.add_parser("command", help="Run a single turn (multi-agent / automation)")
    cmd.add_argument("session_id", help="Session ID to run against")
    cmd.add_argument("prompt", help="The prompt to send")
    cmd.add_argument("--provider", choices=_PROVIDER_CHOICES, default=None)
    cmd.add_argument("--model", default=None)
    cmd.add_argument("--no-tools", action="store_true")
    cmd.add_argument(
        "--disable-delegation",
        action="store_true",
        help="Disable the delegate_task tool to prevent infinite recursion",
    )

    run_task = subparsers.add_parser("run_task", help="Run an agent by ID inside the sessions directory (agent_N)")
    run_task.add_argument("agent_id", help="Agent ID to run (e.g. agent_1)")
    run_task.add_argument("prompt", help="The task prompt")

    subparsers.add_parser("status", help="List sessions")

    sot_a = subparsers.add_parser("sot_attach", help="Attach a path to a session's Source of Truth")
    sot_a.add_argument("session_id")
    sot_a.add_argument("target", help="File or directory path to attach")
    sot_a.add_argument("--label", default=None)
    sot_a.add_argument("--non-recursive", action="store_true")

    sot_s = subparsers.add_parser("sot_show", help="Show Source of Truth entries for a session")
    sot_s.add_argument("session_id")

    sot_d = subparsers.add_parser("sot_delete", help="Remove an entry from the Source of Truth")
    sot_d.add_argument("session_id")
    sot_d.add_argument("ref", help="Short ID or full path of the entry to remove")

    return parser


# ── Status ───────────────────────────────────────────────────────────────


def _print_status(runtime: AppRuntime) -> None:
    records = runtime.sessions.list_sessions()
    if not records:
        console.print("[dim]No sessions found.[/dim]")
        return

    table = Table(title="Sessions")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Updated")
    table.add_column("SoT", justify="right")

    for record in records:
        table.add_row(
            escape(record.id),
            escape(record.title),
            escape(record.provider),
            escape(record.model),
            escape(record.updated_at),
            str(len(record.source_entries)),
        )

    console.print(table)
    console.print("[dim]Resume a session: sot-cli <session_id>[/dim]")


def _clean_sot_session(session_id: str, sessions_dir=None):
    """Remove all SoT data from a session's persisted JSON files.

    Cleans three files:
      1. request.json   — drops every ``user`` message whose content
                          starts with ``=== SOURCE OF TRUTH ===`` or
                          ``=== CURRENT METADATA ===``.
      2. session.json  — empties ``source_entries``.
      3. turn_metadata.json — sets ``SoT Tracked Files`` to 0 and
                               ``sot_files`` to ``[]``.
    """
    from pathlib import Path
    import json
    import shutil

    if sessions_dir is None:
        sessions_dir = Path.cwd() / ".sot-cli"
    sessions_path = Path(sessions_dir) / "sessions"
    session_path = sessions_path / session_id

    if not session_path.is_dir():
        error_console.print(f"[red]Session not found: {escape(session_id)}[/red]")
        return

    cleaned_request = 0
    cleaned_metadata = 0
    cleaned_session = 0

    # --- 1. request.json ---
    request_file = session_path / "request.json"
    if request_file.is_file():
        try:
            request_data = json.loads(request_file.read_text(encoding="utf-8"))
            messages = request_data.get("payload", {}).get("messages", [])
            if isinstance(messages, list):
                SOT_PREFIX = "=== SOURCE OF TRUTH ==="
                META_PREFIX = "=== CURRENT METADATA ==="
                filtered = []
                for msg in messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role == "user" and isinstance(content, str):
                        stripped = content.lstrip()
                        if stripped.startswith(SOT_PREFIX):
                            cleaned_request += 1
                            continue
                        if stripped.startswith(META_PREFIX):
                            cleaned_metadata += 1
                            continue
                    filtered.append(msg)
                if cleaned_request or cleaned_metadata:
                    backup = request_file.with_suffix(".json.bak")
                    shutil.copy2(request_file, backup)
                    request_data["payload"]["messages"] = filtered
                    request_file.write_text(
                        json.dumps(request_data, ensure_ascii=True, indent=2),
                        encoding="utf-8",
                    )
                    console.print(f"[dim]Backup saved: {escape(str(backup))}[/dim]")
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    # --- 2. session.json ---
    session_file = session_path / "session.json"
    if session_file.is_file():
        try:
            session_data = json.loads(session_file.read_text(encoding="utf-8"))
            entries = session_data.get("source_entries", [])
            if entries:
                cleaned_session = len(entries)
                session_data["source_entries"] = []
                session_file.write_text(
                    json.dumps(session_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except (json.JSONDecodeError, OSError):
            pass

    # --- 3. turn_metadata.json ---
    metadata_file = session_path / "turn_metadata.json"
    if metadata_file.is_file():
        try:
            meta = json.loads(metadata_file.read_text(encoding="utf-8"))
            changed = False
            snapshot = meta.get("snapshot", {})
            if snapshot.get("SoT Tracked Files", 0) != 0:
                snapshot["SoT Tracked Files"] = 0
                changed = True
            render = meta.get("render", {})
            if render.get("sot_files", []):
                render["sot_files"] = []
                changed = True
            if changed:
                metadata_file.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except (json.JSONDecodeError, OSError):
            pass

    # --- 4. Report ---
    parts = []
    if cleaned_request:
        parts.append(f"{cleaned_request} SoT {'block' if cleaned_request == 1 else 'blocks'} from request.json")
    if cleaned_metadata:
        parts.append(f"{cleaned_metadata} metadata {'block' if cleaned_metadata == 1 else 'blocks'} from request.json")
    if cleaned_session:
        parts.append(f"{cleaned_session} source {'entry' if cleaned_session == 1 else 'entries'} from session.json")

    if parts:
        console.print(f"[green]Cleaned {escape(session_id)}: {', '.join(parts)}.[/green]")
    else:
        console.print(f"[dim]Session {escape(session_id)} has no SoT data to clean.[/dim]")

def _list_sessions(sessions_dir=None):
    import json
    from pathlib import Path

    if sessions_dir is None:
        sessions_dir = Path.cwd() / ".sot-cli"
    sessions_path = Path(sessions_dir) / "sessions"
    if not sessions_path.is_dir():
        print("SESSIONS:")
        print("[]")
        return

    sessions = []
    for session_dir in sorted(sessions_path.iterdir(), reverse=True):
        if not session_dir.is_dir():
            continue
        session_file = session_dir / "session.json"
        if session_file.is_file():
            try:
                data = json.loads(session_file.read_text(encoding="utf-8"))
                data["id"] = session_dir.name
                sessions.append(data)
            except Exception:
                pass

    print("SESSIONS:")
    print(json.dumps(sessions, indent=2, ensure_ascii=False, default=str))


# ── SoT display ──────────────────────────────────────────────────────────


def _print_sot(runtime: AppRuntime, session_id: str) -> None:
    record = runtime.sessions.load(session_id)
    if not record.source_entries:
        console.print(f"[dim]No SoT entries in session {escape(session_id)}.[/dim]")
        return

    table = Table(title=f"Source of Truth — {escape(session_id)}")
    table.add_column("ID", style="cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Path / Ref", style="green")

    for entry in record.source_entries:
        table.add_row(escape(entry.id), escape(entry.kind), escape(entry.value))

    console.print(table)


# ── Command turn (one-shot) ──────────────────────────────────────────────


async def _run_command_turn(
    runtime: AppRuntime,
    session_id: str,
    prompt: str,
    provider_name: str | None,
    model_override: str | None,
    no_tools: bool,
    disable_delegation: bool = False,
    conversation_state: ConversationState | None = None,
) -> int:
    record = runtime.sessions.load(session_id)
    bundle = build_source_bundle(record)
    request = prepare_turn_request(
        config=runtime.config,
        session=record,
        user_prompt=prompt,
        bundle=bundle,
        provider_name=provider_name,
        model_override=model_override,
        enable_tools=not no_tools,
        disable_delegation=disable_delegation,
    )

    if conversation_state is None:
        from sot_cli.query import ConversationState
        conversation_state = ConversationState()

    
    start_time = time.perf_counter()
    
    result = await run_tool_loop(runtime, request, console, conversation_state=conversation_state)
    
    turn_duration = time.perf_counter() - start_time

    if result.usage:
        agents_dir = runtime.paths.sessions_dir / session_id / "agents"
        agent_statuses = []
        if agents_dir.exists():
            for agent_folder in sorted(agents_dir.iterdir()):
                if agent_folder.is_dir():
                    report_file = agent_folder / "response.md"
                    if report_file.exists():
                        content = report_file.read_text(encoding="utf-8")
                        status_line = next((line for line in content.splitlines() if line.startswith("**Status:**")), "")
                        status = status_line.replace("**Status:**", "").strip() if status_line else "UNKNOWN"
                        agent_statuses.append((agent_folder.name, status))

        usage_table = Table(title="Turn Summary & Usage")
        usage_table.add_column("Metric")
        usage_table.add_column("Value", justify="right")
        usage_table.add_row("Session ID", escape(session_id))

        main_tokens = result.usage.get("total_tokens", 0) - result.usage.get("delegated_total_tokens", 0)
        total_tokens = result.usage.get("total_tokens", 0)
        latest_prompt_tokens = result.usage.get("latest_prompt_tokens")
        
        usage_table.add_row("Main Agent Tokens", str(main_tokens))
        if result.usage.get("delegated_total_tokens"):
            usage_table.add_row("Sub-Agents Tokens", str(result.usage.get("delegated_total_tokens")))
        usage_table.add_row("Total Tokens", str(total_tokens), style="bold cyan")
        usage_table.add_row("Total Cost", f"${result.usage.get('cost', 0.0):.6f}", style="bold green")

        adapter = runtime.provider_adapter(record.provider, record.model)
        
        ctx_len = adapter.capability.allocated_context_length or adapter.capability.context_length
        
        if ctx_len and ctx_len > 0 and isinstance(latest_prompt_tokens, (int, float)):
            pct = min(100, int((latest_prompt_tokens / ctx_len) * 100))
            filled = int((pct / 100) * 10)
            bar = "█" * filled + "░" * (10 - filled)
            color = "red" if pct > 90 else "yellow" if pct > 75 else "green"
            
            label = "Context Limit (Allocated)" if adapter.capability.allocated_context_length else "Context Limit (Max)"
            
            usage_table.add_row(
                escape(label),
                f"[{color}]{bar} {pct}% ({int(latest_prompt_tokens)}/{ctx_len})[/{color}]",
            )
            
            if pct >= 90:
                usage_table.add_row("Warning", "[bold red]⚠️ Context almost full! Ask the model to remove some not used SoT files (If Any) [/bold red]")
            elif pct >= 75:
                usage_table.add_row("Warning", "[bold yellow]⚠️ Context is getting full. Consider detaching unused files.[/bold yellow]")

        sot_files = set(conversation_state.sot.tracked_files.keys()).union(conversation_state.sot.tracked_media.keys())
        if sot_files:
            try:
                usage_table.add_section()
            except Exception:
                pass
            usage_table.add_row("SoT Tracked Files", "Always updated in real time => " + str(len(sot_files)), style="bold magenta")
            for fpath in sorted(sot_files):
                fname = Path(fpath).name
                usage_table.add_row(f"  📄 {escape(fname)}", "[dim]in context[/dim]")
            
        if agent_statuses:
            try:
                usage_table.add_section()
            except Exception:
                pass
            usage_table.add_row("Agents Used", str(len(agent_statuses)))
            for agent_name, status in agent_statuses:
                color = "green" if status.upper() == "SUCCESS" else "red"
                usage_table.add_row(escape(f"  {agent_name}"), f"[{color}]{escape(str(status))}[/{color}]")
                
        try:
            usage_table.add_section()
        except Exception:
            pass
            
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        h = int(turn_duration // 3600)
        m = int((turn_duration % 3600) // 60)
        s = turn_duration % 60
        duration_str = f"{h:02d}:{m:02d}:{s:06.3f}" if h > 0 else f"{m:02d}:{s:06.3f}"
        
        usage_table.add_row("Timestamp", escape(now_str))
        usage_table.add_row("Turn Duration", escape(duration_str), style="bold yellow")

        console.print(usage_table)

        meta_snapshot: dict[str, Any] = {
            "Session ID": session_id,
            "Main Agent Tokens": main_tokens,
            "Total Tokens": total_tokens,
            "Total Cost": f"${result.usage.get('cost', 0.0):.6f}",
            "Timestamp": now_str,
            "Turn Duration": duration_str,
        }
        if result.usage.get("delegated_total_tokens"):
            meta_snapshot["Sub-Agents Tokens"] = result.usage.get("delegated_total_tokens")
        if ctx_len and ctx_len > 0 and isinstance(latest_prompt_tokens, (int, float)):
            pct_raw = min(100, int((latest_prompt_tokens / ctx_len) * 100))
            meta_snapshot["Context"] = f"{pct_raw}% ({int(latest_prompt_tokens)}/{ctx_len})"
        if sot_files:
            meta_snapshot["SoT Tracked Files"] = len(sot_files)
        if agent_statuses:
            meta_snapshot["Agents Used"] = len(agent_statuses)
        meta_snapshot["launch_context"] = detect_launch_context()
        conversation_state.last_turn_metadata = meta_snapshot

        render_extras: dict[str, Any] = {
            "sot_files": sorted(sot_files) if sot_files else [],
            "agents": [list(t) for t in agent_statuses] if agent_statuses else [],
        }
        if ctx_len and ctx_len > 0 and isinstance(latest_prompt_tokens, (int, float)):
            render_extras["ctx_pct"] = min(100, int((latest_prompt_tokens / ctx_len) * 100))
            render_extras["ctx_prompt"] = int(latest_prompt_tokens)
            render_extras["ctx_max"] = int(ctx_len)
            render_extras["ctx_label"] = (
                "Context Limit (Allocated)"
                if adapter.capability.allocated_context_length
                else "Context Limit (Max)"
            )

        _save_last_turn_metadata(
            runtime.paths.sessions_dir / session_id,
            meta_snapshot,
            render_extras,
        )

    return 0


async def _run_task(runtime: AppRuntime, agent_id: str, prompt: str) -> int:
    try:
        record = runtime.sessions.load(agent_id)
        agent_dir = runtime.sessions.sessions_dir / agent_id

        bundle = SourceBundle()
        request = prepare_turn_request(
            config=runtime.config,
            session=record,
            user_prompt=prompt,
            bundle=bundle,
            enable_tools=True,
            disable_delegation=True,
        )

        result = await run_tool_loop(runtime, request, console, is_task=True)
        result_text_value = getattr(result, "text", "") or ""
        finished_reason = getattr(result, "finished_reason", "") or ""
        if getattr(result, "is_error", False):
            status, result_text, error_text = "error", "", result_text_value
        elif finished_reason == "length":
            # Provider cut the sub-agent off mid-response. The partial text the
            # model managed to emit is preserved in the Error Log so the boss
            # sees what was lost, not just a SUCCESS with a truncated body.
            status = "error"
            result_text = ""
            error_text = (
                "Sub-agent response was cut off by the provider's max_output_tokens (finish_reason=length). "
                "The work is incomplete. Partial text emitted before the cut:\n\n"
                + result_text_value
            ).rstrip()
        elif not result_text_value.strip():
            # Empty terminal response: no text, no tool calls, no exception.
            # Could be a silent provider failure, a dead connection that
            # returned 200-empty, or the model simply choosing to stop. From
            # the boss's perspective these are indistinguishable, and "no
            # result provided" is the honest summary.
            status = "error"
            result_text = ""
            error_text = (
                "Sub-agent terminated without producing any content "
                "(empty model response, no tool calls, no exception). "
                "Treat as inconclusive — re-delegate with a sharper task_prompt "
                "or execute the step directly."
            )
        else:
            status, result_text, error_text = "success", result_text_value, ""
        usage = getattr(result, "usage", {})
    except Exception as e:
        status, result_text, error_text = "error", "", str(e)
        usage = {}

    from sot_cli.tools.session.delegate import _write_response_md
    parent_session_dir = runtime.sessions.sessions_dir.parent
    _write_response_md(parent_session_dir, agent_id, status, prompt, result_text, usage, error_text)
    return 0 if status == "success" else 1


# ── Interactive prompt ───────────────────────────────────────────────────


async def _run_prompt(
    runtime: AppRuntime,
    session_id: str | None,
    title: str | None,
    provider_name: str | None,
    model_override: str | None,
    no_tools: bool,
    subagent_model_override: str | None = None,
    hypercompress: bool = False,
) -> int:
    if session_id:
        record = runtime.sessions.load(session_id)

        if not provider_name and sys.stdin.isatty():
            available = [name for name, cfg in runtime.config.providers.items() if cfg.enabled]
            if len(available) > 1:
                console.print(
                    f"\n[dim]Resuming session [/dim][bold]{escape(record.id)}[/bold] "
                    f"[dim](current provider: [/dim]"
                    f"[bold yellow]{escape(record.provider)}[/bold yellow][dim])[/dim]"
                )
                provider_name = _select_provider_interactive(available, current_default=record.provider)

        updated_provider = provider_name or record.provider
        provider_config = runtime.config.provider(updated_provider)
        provider_changed = updated_provider != record.provider

        if model_override:
            updated_model = model_override
        elif provider_changed:
            updated_model = provider_config.model
            if not updated_model and updated_provider not in {"lmstudio", "ollama"}:
                raise ValueError(
                    f"Provider {updated_provider} has no default model configured. Pass --model explicitly."
                )
        else:
            updated_model = provider_config.model or record.model
            if not updated_model and updated_provider not in {"lmstudio", "ollama"}:
                raise ValueError(
                    f"Provider {updated_provider} has no default model configured. Pass --model explicitly."
                )

        update_kwargs: dict[str, Any] = {}
        if provider_changed:
            update_kwargs["provider"] = updated_provider
        # Always reset temperature/max_output_tokens to None so they
        # dynamically inherit from sot.toml on each resume, unless the
        # model explicitly overrides them via update_session.
        update_kwargs["temperature"] = None
        update_kwargs["max_output_tokens"] = None
        if updated_model != record.model:
            update_kwargs["model"] = updated_model

        if update_kwargs:
            record = runtime.sessions.update_session(session_id, **update_kwargs)

        # Update subagent_model on resume if CLI override provided
        if subagent_model_override:
            record = runtime.sessions.update_session(session_id, subagent_model=subagent_model_override)
    else:
        provider = provider_name or runtime.config.runtime.primary_provider
        provider_config = runtime.config.provider(provider)
        model = model_override or provider_config.model
        if not model and provider not in {"lmstudio", "ollama"}:
            raise ValueError(f"Provider {provider} has no default model configured. Pass --model explicitly.")
        session_title = title or f"prompt {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        # Resolve subagent_model: CLI override > provider config default
        resolved_subagent = subagent_model_override or provider_config.subagent_model or ""
        record = runtime.sessions.create_session(
            session_title,
            provider=provider,
            model=model,
            temperature=None,
            max_output_tokens=None,
            subagent_model=resolved_subagent,
        )

    active_provider = record.provider
    active_model = record.model

    adapter = await runtime.provider_adapter_async(active_provider, active_model)
    stats_line, caps_line = _format_capability_line(adapter.capability)

    if not active_model and adapter.model:
        active_model = adapter.model
        record = runtime.sessions.update_session(record.id, model=active_model)

    start_now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logo = (
        "[bold cyan]"
        " ▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄      ▄▄▄▄▄▄▄  ▄        ▄▄▄▄▄▄▄ \n"
        "▐░░░░░░░▌▐░░░░░░░▌▐░░░░░░░▌    ▐░░░░░░░▌▐░▌      ▐░░░░░░░▌\n"
        "▐░█▀▀▀▀▀ ▐░█▀▀▀█░▌ ▀▀█░█▀▀     ▐░█▀▀▀▀▀ ▐░▌       ▀▀█░█▀▀ \n"
        "▐░█▄▄▄▄▄ ▐░▌   ▐░▌   ▐░▌ ▄▄▄▄▄ ▐░▌      ▐░▌         ▐░▌   \n"
        "▐░░░░░░░▌▐░▌   ▐░▌   ▐░▌▐░░░░░▌▐░▌      ▐░▌         ▐░▌   \n"
        " ▀▀▀▀▀█░▌▐░▌   ▐░▌   ▐░▌ ▀▀▀▀▀ ▐░▌      ▐░▌         ▐░▌   \n"
        " ▄▄▄▄▄█░▌▐░█▄▄▄█░▌   ▐░▌       ▐░█▄▄▄▄▄ ▐░█▄▄▄▄▄  ▄▄█░█▄▄ \n"
        "▐░░░░░░░▌▐░░░░░░░▌   ▐░▌       ▐░░░░░░░▌▐░░░░░░░▌▐░░░░░░░▌\n"
        " ▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀     ▀         ▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀ \n"
        "[/bold cyan]"
    )

    provider_base_url = runtime.config.provider(active_provider).base_url
    line_provider = f"[bold white]provider[/bold white] [bold yellow]{escape(active_provider.upper())}[/bold yellow]"
    line_session = f"[bold white]session[/bold white]  {escape(record.id)}"
    line_route = f"[bold white]route[/bold white]    {escape(provider_base_url)}" if provider_base_url else ""
    line_model = f"[bold white]model[/bold white]    {escape(active_model)}"
    if any(k in active_model.lower() for k in ["uncensored", "uncensor", "abliterated", "obliterated", "nsfw"]) and "😎" not in active_model:
        line_model += " 😎"
    line_subagent = ""
    if record.subagent_model:
        subagent_display = f"[bold white]subagent[/bold white] {escape(record.subagent_model)}"
        if any(k in record.subagent_model.lower() for k in ["uncensored", "uncensor", "abliterated", "obliterated", "nsfw"]) and "😎" not in record.subagent_model:
            subagent_display += " 😎"
        if record.subagent_model != active_model:
            subagent_display += " (≠ main)"
        line_subagent = subagent_display
    line_time = f"[bold white]started[/bold white]  {escape(start_now_str)}"
    line_stats = f"[bold white]specs[/bold white]    {escape(stats_line)}" if stats_line else ""
    line_caps = f"[bold white]caps[/bold white]     {escape(caps_line)} | tools={'off' if no_tools else 'on'}" if caps_line else f"[bold white]tools[/bold white]    {'off' if no_tools else 'on'}"

    os_name = _normalize_os_name(platform.system()) or "Unknown"
    os_release = platform.release() or ""
    arch = _normalize_arch(platform.machine()) or ""
    hostname = ""
    try:
        hostname = socket.gethostname()
    except Exception:
        pass
    username = ""
    try:
        username = getpass.getuser()
    except Exception:
        username = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    shell = _detect_active_shell() or "unknown"
    cwd = ""
    try:
        cwd = os.getcwd()
    except Exception:
        pass

    host_parts = [f"{os_name} {os_release}".strip(), arch]
    if hostname:
        host_parts.append(hostname)
    if username:
        host_parts.append(f"user={username}")
    line_host = f"[bold white]host[/bold white]     {escape(' | '.join(p for p in host_parts if p))}"
    line_shell = f"[bold white]shell[/bold white]    {escape(shell)}"
    line_cwd = f"[bold white]cwd[/bold white]      {escape(cwd)}" if cwd else ""

    banner_lines = [logo, ""]
    for line in [line_provider, line_session, line_route, line_model, line_subagent, line_time, line_stats, line_caps, "", line_host, line_shell, line_cwd]:
        if line or line == "":
            banner_lines.append(line)
    banner_lines.append("")
    banner_lines.append(
        "[yellow]tip: edit [bold]sot.toml[/bold] (base URLs, models, runtime options) and "
        "[bold]sot.keys.toml[/bold] (API keys) to change provider settings.[/yellow]"
    )
    banner_lines.append("")
    banner_lines.append(f"[bold yellow]{_submit_shortcut_help_text()} Press Ctrl+C on an empty prompt to leave.[/bold yellow]")

    console.print(
        Panel.fit(
            "\n".join(banner_lines),
            border_style="cyan",
        )
    )

    kb = KeyBindings()
    current_turn_task: asyncio.Task[int] | None = None
    turn_interrupt_requested = False
    previous_sigint_handler = signal.getsignal(signal.SIGINT)

    @kb.add("enter")
    def _handle_enter(event) -> None:
        if event.data in _CTRL_ENTER_DATA:
            event.current_buffer.validate_and_handle()
            return
        event.current_buffer.insert_text("\n")

    @kb.add("escape", "enter")
    def _submit_multiline(event) -> None:
        event.current_buffer.validate_and_handle()

    @kb.add("c-c")
    def _handle_prompt_ctrl_c(event) -> None:
        if event.current_buffer.text.strip():
            event.current_buffer.reset()
            return
        event.app.exit(exception=KeyboardInterrupt, style="class:exiting")

    def _handle_sigint() -> None:
        """Body of the Ctrl+C response. Same effect whether we got here via the
        asyncio loop's signal integration (Unix) or the plain `signal.signal`
        handler (Windows)."""
        nonlocal current_turn_task, turn_interrupt_requested
        try:
            if try_interrupt_active_foreground():
                try:
                    sys.stderr.write(
                        "\n\x1b[33mForeground run_command interrupted by user (Ctrl+C). "
                        "Model will continue.\x1b[0m\n"
                    )
                    sys.stderr.flush()
                except Exception:
                    pass
                return
        except Exception:
            pass
        if current_turn_task is not None and not current_turn_task.done():
            turn_interrupt_requested = True
            current_turn_task.cancel()
            return
        raise KeyboardInterrupt()

    def _handle_sigint_sync(_signum, _frame) -> None:
        # Adapter for the (signum, frame) signature that signal.signal
        # requires. add_signal_handler passes no arguments.
        _handle_sigint()

    # The asyncio loop's native signal integration is what actually makes
    # Ctrl+C cancel the model turn during long awaits on httpx. It does TWO
    # things: registers our callback AND wires asyncio's set_wakeup_fd so
    # the selector exits the moment a signal arrives. Plain signal.signal
    # only does the first, which is why the bare-signal path fails to wake
    # the loop while it's parked waiting for the first byte from the model.
    # On Windows add_signal_handler raises NotImplementedError; fall back to
    # plain signal.signal there.
    loop = asyncio.get_running_loop()
    sigint_via_loop = False
    try:
        loop.add_signal_handler(signal.SIGINT, _handle_sigint)
        sigint_via_loop = True
    except (NotImplementedError, RuntimeError):
        signal.signal(signal.SIGINT, _handle_sigint_sync)

    prompt_session = PromptSession(key_bindings=kb, multiline=True)

    session_state = ConversationState()
    session_dir = runtime.paths.sessions_dir / record.id
    loaded_history = _load_chat_history_from_request_jsons(session_dir)
    if loaded_history:
        session_state.chat_history = loaded_history
        _replay_conversation(loaded_history)
    loaded_sot = load_sot_state_from_request_json(session_dir)
    if loaded_sot is not None:
        session_state.sot = loaded_sot

    # Hyper-compress if requested
    if hypercompress:
        hc_result = hyper_compress_session(session_dir, dry_run=False)
        if "error" not in hc_result:
            console.print(f"[green]Hyper-compressed: {hc_result['messages_before']} → {hc_result['messages_after']} messages, saved ~{hc_result['chars_saved']:,} chars[/green]")
            # Reload compressed history into memory so subsequent turns use the clean state
            compressed_history = _load_chat_history_from_request_jsons(session_dir)
            if compressed_history:
                session_state.chat_history = compressed_history
        else:
            console.print(f"[yellow]Hyper-compress skipped: {hc_result['error']}[/yellow]")

    loaded_meta = _load_last_turn_metadata(session_dir)
    if loaded_meta:
        loaded_snapshot = loaded_meta.get("snapshot") or {}
        loaded_render = loaded_meta.get("render") or {}
        if loaded_snapshot:
            session_state.last_turn_metadata = loaded_snapshot
        _render_resumed_summary(loaded_snapshot, loaded_render, record.id)

    try:
        while True:
            try:
                prompt = await prompt_session.prompt_async(
                    HTML("<b><cyan>you&gt;</cyan></b> "),
                    prompt_continuation="",
                )
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]chat ended[/dim]")
                return 0

            prompt = prompt.rstrip()
            normalized_prompt = prompt.strip()
            if not normalized_prompt:
                continue

            # prompt_toolkit takes over SIGINT during prompt_async() and on
            # exit calls loop.remove_signal_handler(SIGINT). That clears
            # both our callback AND asyncio's wakeup_fd wiring, so the
            # selector no longer wakes when SIGINT arrives — exactly the
            # "Ctrl+C prints ^C but the model keeps going" symptom.
            # Re-installing via add_signal_handler restores both pieces.
            if sigint_via_loop:
                try:
                    loop.add_signal_handler(signal.SIGINT, _handle_sigint)
                except (NotImplementedError, RuntimeError):
                    pass
            else:
                signal.signal(signal.SIGINT, _handle_sigint_sync)

            current_record = runtime.sessions.load(record.id)
            active_provider = current_record.provider
            active_model = current_record.model
            current_turn_task = asyncio.create_task(
                _run_command_turn(
                    runtime, record.id, prompt,
                    active_provider, active_model, no_tools,
                    conversation_state=session_state,
                )
            )
            try:
                await current_turn_task
            except asyncio.CancelledError:
                if not turn_interrupt_requested:
                    raise
                console.print("\n[bold yellow]Turn aborted by user (Ctrl+C).[/bold yellow]")
            except Exception as exc:
                error_console.print(f"\n[bold red]System Error:[/bold red] {escape(str(exc))}")
                console.print("[dim]The session is still active. You can try again or change your prompt.[/dim]")
            finally:
                current_turn_task = None
                turn_interrupt_requested = False
    finally:
        if sigint_via_loop:
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError):
                pass
        else:
            signal.signal(signal.SIGINT, previous_sigint_handler)
