from __future__ import annotations

import asyncio
import json
from typing import Any

from sot_cli.config.prompts import (
    FILE_IN_SOT_STUB,
    FILE_UNCHANGED_STUB,
)
from sot_cli.providers.base import ProviderCapability
from sot_cli.runtime import AppRuntime
from sot_cli.tools.core import ToolExecutionResult, ToolPayload
from sot_cli.tools.editor.apply_edits import execute_edit_files
from sot_cli.tools.editor.write import execute_write_file
from sot_cli.tools.fs.delete import execute_delete_file
from sot_cli.tools.fs.list_dir import execute_list_dir
from sot_cli.tools.reader.main import execute_read_many_files
from sot_cli.tools.search.search_code import execute_search_code
from sot_cli.tools.session.control import (
    execute_attach_path,
    execute_clean_sot,
    execute_detach_path,
    execute_get_session_state,
    execute_update_session,
)
from sot_cli.tools.session.list_tasks import execute_list_tasks, execute_wait_task
from sot_cli.tools.shell.open_path import execute_open_path
from sot_cli.tools.schemas import get_tool_schemas
from sot_cli.tools.browser.browse import (
    execute_browser_back,
    execute_browser_click,
    execute_browser_close,
    execute_browser_forward,
    execute_browser_get_html,
    execute_browser_get_text,
    execute_browser_key,
    execute_browser_navigate,
    execute_browser_open,
    execute_browser_screenshot,
    execute_browser_scroll,
    execute_browser_tab_list,
    execute_browser_tab_new,
    execute_browser_tab_switch,
    execute_browser_type,
)
from sot_cli.tools.shell.run_command import execute_run_command


class ToolRegistry:
    def __init__(
        self,
        runtime: AppRuntime,
        session_id: str,
        capability: ProviderCapability,
        model: str,
        disable_delegation: bool = False,
        sot_state: Any = None,  # <--- AÑADIR ESTO
        context_info: dict[str, Any] | None = None,
    ) -> None:
        self.runtime = runtime
        self.session_id = session_id
        self.capability = capability
        self.model = model
        self.disable_delegation = disable_delegation
        self.sot_state = sot_state  # <--- AÑADIR ESTO
        self.context_info = context_info
        self._read_cache: dict[tuple[str, str | None, int | None, int | None], tuple[int, dict[str, Any]]] = {}

    def schemas(self) -> list[dict[str, Any]]:
        schemas = get_tool_schemas()
        if self.disable_delegation:
            schemas = [schema for schema in schemas if schema.get("function", {}).get("name") != "delegate_task"]
       
        try:
            schemas.extend(self.runtime.mcp.get_tool_schemas())
        except Exception as exc:
            import sys
            sys.stderr.write(f"\n[Warning] Failed to load MCP schemas: {exc}\n")
            sys.stderr.flush()
        return schemas

    async def execute_tool_call(self, tool_call: dict[str, Any]) -> tuple[str, ToolExecutionResult]:
        function = tool_call.get("function") or {}
        name = str(function.get("name", "")).strip()
        raw_arguments = function.get("arguments") or "{}"
        tool_call_id = str(tool_call.get("id", ""))

        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            error_content = json.dumps({"ok": False, "error": f"Invalid tool arguments JSON: {exc}"}, ensure_ascii=True)
            return tool_call_id, ToolExecutionResult(
                name=name or "unknown",
                content=error_content,
                record_content=error_content,
                supplemental_messages=[],
                is_error=True,
            )

        handlers = {
            "list_dir": self._list_dir,
            "read_files": self._read_files,
            "search_code": self._search_code,
            "open_path": self._open_path,
            "run_command": self._run_command,
            "wait_task": self._wait_task,
            "edit_files": self._edit_files,
            "write_file": self._write_file,
            "delete_file": self._delete_file,
            "get_session_state": self._get_session_state,
            "update_session": self._update_session,
            "detach_path_from_source": self._detach_path_from_source,
            "attach_path_to_source": self._attach_path_to_source,
            "list_tasks": self._list_tasks,
            "clean_sot": self._clean_sot,
            "browser_open": self._browser_open,
            "browser_close": self._browser_close,
            "browser_navigate": self._browser_navigate,
            "browser_screenshot": self._browser_screenshot,
            "browser_click": self._browser_click,
            "browser_type": self._browser_type,
            "browser_key": self._browser_key,
            "browser_scroll": self._browser_scroll,
            "browser_get_html": self._browser_get_html,
            "browser_get_text": self._browser_get_text,
            "browser_back": self._browser_back,
            "browser_forward": self._browser_forward,
            "browser_tab_new": self._browser_tab_new,
            "browser_tab_list": self._browser_tab_list,
            "browser_tab_switch": self._browser_tab_switch,
        }
        # Only expose delegate_task when delegation is allowed for this registry/session.
        if not self.disable_delegation:
            handlers["delegate_task"] = self._delegate_task
        handler = handlers.get(name)
        # Check MCP tools if no local handler
        if handler is None and getattr(self.runtime, "mcp", None) is not None and self.runtime.mcp.is_mcp_tool(name):
            try:
                mcp_result = await self.runtime.mcp.call_tool(name, arguments)
                record_content = json.dumps({"ok": True, **mcp_result}, ensure_ascii=True)
                return tool_call_id, ToolExecutionResult(
                    name=name, content=record_content, record_content=record_content,
                    supplemental_messages=[], is_error=False,
                )
            except Exception as exc:
                error_content = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True)
                return tool_call_id, ToolExecutionResult(
                    name=name, content=error_content, record_content=error_content,
                    supplemental_messages=[], is_error=True,
                )

        if handler is None:
            error_content = json.dumps({"ok": False, "error": f"Unknown tool: {name}"}, ensure_ascii=True)
            return tool_call_id, ToolExecutionResult(
                name=name or "unknown",
                content=error_content,
                record_content=error_content,
                supplemental_messages=[],
                is_error=True,
            )

        try:
            # Local tool handlers are synchronous and some of them (run_command
            # foreground, large reads, search_code) block for many seconds. If
            # we ran them inline on the asyncio event loop we would freeze the
            # whole runtime — including MCP stdio/websocket clients that must
            # service keep-alives in the background. Hand the call off to a
            # worker thread so the event loop keeps spinning.
            raw_result = await asyncio.to_thread(handler, arguments)
            if isinstance(raw_result, ToolPayload):
                payload = raw_result.payload
                record_content = json.dumps({"ok": True, **payload}, ensure_ascii=True)
                content = raw_result.model_content or record_content
                supplemental_messages = raw_result.supplemental_messages
            else:
                payload = raw_result
                record_content = json.dumps({"ok": True, **payload}, ensure_ascii=True)
                content = record_content
                supplemental_messages = []

            return tool_call_id, ToolExecutionResult(
                name=name,
                content=content,
                record_content=record_content,
                supplemental_messages=supplemental_messages,
                is_error=False,
            )
        except Exception as exc:
            error_content = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True)
            return tool_call_id, ToolExecutionResult(
                name=name,
                content=error_content,
                record_content=error_content,
                supplemental_messages=[],
                is_error=True,
            )

    # ── thin delegating methods ───────────────────────────────────────────

    def _list_dir(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_list_dir(arguments, self.runtime.paths.root_dir)

    def _read_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_read_many_files(
            arguments,
            root_dir=self.runtime.paths.root_dir,
            read_cache=self._read_cache,
            binary_check_size=self.runtime.config.tools.binary_check_size,
            supports_images=self.capability.supports_images,
            supports_pdf=self.capability.supports_pdfs,
            supports_audio=self.capability.supports_audio,
            supports_video=self.capability.supports_video,
            file_unchanged_stub=FILE_UNCHANGED_STUB,
            sot_state=self.sot_state,
            file_in_sot_stub=FILE_IN_SOT_STUB,
            context_info=self.context_info,
            max_readable_file_tokens=self.runtime.config.tools.max_readable_file_tokens,
        )

    def _search_code(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tools_cfg = self.runtime.config.tools
        return execute_search_code(
            arguments,
            self.runtime.paths.root_dir,
            default_head_limit=tools_cfg.search_default_head_limit,
            max_line_length=tools_cfg.search_max_line_length,
            timeout_seconds=tools_cfg.search_timeout_seconds,
        )

    def _run_command(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_run_command(
            arguments,
            root_dir=self.runtime.paths.root_dir,
            logs_dir=self.runtime.paths.logs_dir,
            session_id=self.session_id,
            default_command_timeout_seconds=self.runtime.config.tools.default_command_timeout_seconds,
        )

    def _open_path(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_open_path(arguments, self.runtime.paths.root_dir)

    def _edit_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_edit_files(arguments, self.runtime.paths.root_dir)

    def _write_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_write_file(arguments, self.runtime.paths.root_dir)

    def _delete_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_delete_file(arguments, self.runtime.paths.root_dir)

    def _get_session_state(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_get_session_state(arguments, self.runtime, self.session_id, self.sot_state)  # <--- AÑADIR self.sot_state

    def _update_session(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_update_session(arguments, self.runtime, self.session_id)

    def _clean_sot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_clean_sot(arguments, self.runtime, self.session_id)

    def _detach_path_from_source(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_detach_path(arguments, self.runtime, self.session_id, self.runtime.paths.root_dir)

    def _attach_path_to_source(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_attach_path(arguments, self.runtime, self.session_id, self.runtime.paths.root_dir)


    def _delegate_task(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from sot_cli.tools.session.delegate import execute_delegate_task

        return execute_delegate_task(arguments, self.runtime, self.session_id)


    def _list_tasks(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_list_tasks(arguments, self.runtime.paths.sessions_dir, self.session_id)

    def _wait_task(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_wait_task(arguments, self.runtime.paths.sessions_dir, self.session_id)

    def _browser_open(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_open(arguments)

    def _browser_close(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_close(arguments)

    def _browser_navigate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_navigate(arguments)

    def _browser_screenshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_screenshot(arguments)

    def _browser_click(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_click(arguments)

    def _browser_type(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_type(arguments)

    def _browser_key(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_key(arguments)

    def _browser_scroll(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_scroll(arguments)

    def _browser_get_html(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_get_html(arguments)

    def _browser_get_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = execute_browser_get_text(arguments)
        return result

    def _browser_back(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_back(arguments)

    def _browser_forward(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_forward(arguments)

    def _browser_tab_new(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_tab_new(arguments)

    def _browser_tab_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_tab_list(arguments)

    def _browser_tab_switch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return execute_browser_tab_switch(arguments)
