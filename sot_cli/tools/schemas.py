from __future__ import annotations

from typing import Any

from sot_cli.config import KNOWN_PROVIDERS
from sot_cli.config.prompts import (
    ATTACH_PATH_PROMPT,
    BROWSE_BACK_PROMPT,
    BROWSE_CLICK_PROMPT,
    BROWSE_CLOSE_PROMPT,
    BROWSE_FORWARD_PROMPT,
    BROWSE_GET_HTML_PROMPT,
    BROWSE_GET_TEXT_PROMPT,
    BROWSE_KEY_PROMPT,
    BROWSE_NAVIGATE_PROMPT,
    BROWSE_OPEN_PROMPT,
    BROWSE_SCREENSHOT_PROMPT,
    BROWSE_SCROLL_PROMPT,
    BROWSE_TAB_LIST_PROMPT,
    BROWSE_TAB_NEW_PROMPT,
    BROWSE_TAB_SWITCH_PROMPT,
    BROWSE_TYPE_PROMPT,
    CLEAN_SOT_PROMPT,
    DELEGATE_TASK_PROMPT,
    DELETE_FILE_PROMPT,
    DETACH_PATH_PROMPT,
    EDIT_FILES_PROMPT,
    GET_SESSION_STATE_PROMPT,
    LIST_DIR_PROMPT,
    LIST_TASKS_PROMPT,
    OPEN_PATH_PROMPT,
    READ_MANY_FILES_PROMPT,
    RUN_COMMAND_PROMPT,
    SEARCH_CODE_PROMPT,
    UPDATE_SESSION_PROMPT,
    WAIT_TASK_PROMPT,
    WRITE_FILE_PROMPT,
)


def get_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": LIST_DIR_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or project-relative path to the directory."},
                        "follow_symlinks": {"type": "boolean", "description": "If true, recurse through symlinked directories."},
                        "recursive": {"type": "boolean", "description": "If true, recurse into subdirectories. Default false."},
                        "kind": {"type": "string", "enum": ["file", "directory", "symlink", "symlink_file", "symlink_directory"], "description": "Optional kind filter."},
                        "extensions": {"type": "array", "items": {"type": "string"}, "description": "Optional extension filter list. Accept values like '.png' or 'png'. Case-insensitive."},
                        "name_contains": {"type": "string", "description": "Optional case-insensitive substring filter applied to the basename. Supports multiple keywords separated by commas (e.g., 'File1, fILE2, etc') acting as an OR condition."},
                        "path_contains": {"type": "string", "description": "Optional case-insensitive substring filter applied to the relative path and absolute path."},
                        "name_pattern": {"type": "string", "description": "Optional basename glob pattern using wildcards like '*', '?', and '[]'. Case-insensitive."},
                        "path_pattern": {"type": "string", "description": "Optional relative-path or absolute-path glob pattern using wildcards like '*', '?', and '[]'. Case-insensitive."},
                        "content_contains": {"type": "string", "description": "Optional text search inside file contents (UTF-8 text files). Supports multiple keywords separated by commas as OR."},
                        "content_case_sensitive": {"type": "boolean", "description": "If true, content_contains is case-sensitive. Default false."},
                        "content_max_bytes": {"type": "integer", "minimum": 0, "description": "Optional maximum file size in bytes for content_contains scanning. Files above this size are skipped. 0 means no size cap."},
                        "min_size_bytes": {"type": "integer", "minimum": 0, "description": "Optional minimum size in bytes."},
                        "max_size_bytes": {"type": "integer", "minimum": 0, "description": "Optional maximum size in bytes."},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_files",
                "description": READ_MANY_FILES_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "description": "List of file read requests to execute together.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "Absolute path or project-relative path to the file."},
                                    "pages": {"type": "string", "description": "Optional PDF page range like '1-5' or '3'. Only valid for PDF files."},
                                    "password": {"type": "string", "description": "Optional password for encrypted/protected PDF files."},
                                    "force": {"type": "boolean", "description": "Bypass context-size warning and read the file anyway. Use only when you are certain the file fits in the remaining context."},
                                },
                                "required": ["path"],
                                "additionalProperties": False,
                            },
                            "minItems": 1,
                        },
                    },
                    "required": ["files"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "open_path",
                "description": OPEN_PATH_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or project-relative path to the file or directory to open."},
                        "application": {"type": "string", "description": "Optional app name, command name, or executable path to use instead of the OS default application."},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_code",
                "description": SEARCH_CODE_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Regular expression pattern to search for in file contents."},
                        "path": {"type": "string", "description": "File or directory to search in. Defaults to project root."},
                        "glob": {"type": "string", "description": "Glob pattern to filter files (e.g. \"*.py\", \"*.{ts,tsx}\")."},
                        "type": {"type": "string", "description": "File type to search (e.g., \"py\", \"js\", \"rust\", \"go\", \"java\")."},
                        "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"], "description": "Output mode: \"content\" shows matching lines, \"files_with_matches\" shows file paths (default), \"count\" shows match counts."},
                        "context_before": {"type": "integer", "minimum": 0, "description": "Lines of context to show before each match (content mode only)."},
                        "context_after": {"type": "integer", "minimum": 0, "description": "Lines of context to show after each match (content mode only)."},
                        "context": {"type": "integer", "minimum": 0, "description": "Lines of context before and after each match (content mode only). Overrides context_before/context_after."},
                        "show_line_numbers": {"type": "boolean", "description": "Show line numbers in content mode output. Defaults to true."},
                        "case_insensitive": {"type": "boolean", "description": "Case insensitive search. Defaults to false."},
                        "head_limit": {"type": "integer", "minimum": 0, "description": "Max result lines/entries. Defaults to 200. Pass 0 for unlimited."},
                        "offset": {"type": "integer", "minimum": 0, "description": "Skip first N results before applying head_limit. For pagination."},
                        "multiline": {"type": "boolean", "description": "Enable multiline mode where patterns can span lines. Defaults to false."},
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": RUN_COMMAND_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to run."},
                        "stdin": {"type": "string", "description": "Optional text to feed to the process stdin. Use for passwords, interactive prompts, or piped input."},
                        "cwd": {"type": "string", "description": "Optional absolute or project-relative working directory."},
                        "timeout_seconds": {"type": "integer", "minimum": 0, "description": "Optional timeout in seconds. 0 = no timeout (infinite). Default is taken from sot.toml [tools].default_command_timeout_seconds = 180."},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        },
        _edit_files_schema(),
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": WRITE_FILE_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or project-relative path to the file."},
                        "content": {"type": "string", "description": "Full UTF-8 text content to write."},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_file",
                "description": DELETE_FILE_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or project-relative path to the file or directory."},
                        "recursive": {"type": "boolean", "description": "Required for deleting directories that are not symlinks."},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_session_state",
                "description": GET_SESSION_STATE_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_session",
                "description": UPDATE_SESSION_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Optional new title for the session."},
                        "provider": {"type": "string", "enum": list(KNOWN_PROVIDERS), "description": "Optional provider to use for future turns in this session."},
                        "model": {"type": "string", "description": "Optional model to use for future turns in this session."},
                        "temperature": {"type": "number", "description": "Optional temperature override for future turns in this session."},
                        "max_output_tokens": {"type": "integer", "minimum": 1, "description": "Optional max output tokens override for future turns in this session."},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "detach_path_from_source",
                "description": DETACH_PATH_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or project-relative path already attached to the session source of truth."},
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": "Batch removal: absolute paths or project-relative paths already attached to the session source of truth. Prefer this over multiple calls when detaching several paths."
                        },
                    },
                    "additionalProperties": False,
                    "oneOf": [
                        {"required": ["path"]},
                        {"required": ["paths"]}
                    ],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "attach_path_to_source",
                "description": ATTACH_PATH_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute or project-relative path to attach."},
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": "Batch attach: absolute or project-relative paths to attach. Prefer this over multiple calls when attaching several paths."
                        },
                        "recursive": {"type": "boolean", "description": "Whether a directory attach should recurse. Applies to every path in the batch."},
                        "label": {"type": "string", "description": "Optional human label. Only supported when attaching a single path."},
                    },
                    "additionalProperties": False,
                    "oneOf": [
                        {"required": ["path"]},
                        {"required": ["paths"]}
                    ],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_tasks",
                "description": LIST_TASKS_PROMPT,
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "wait_task",
                "description": WAIT_TASK_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string", "description": "The agent_id to wait for (e.g., agent_1)."},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "description": "Optional maximum time to wait in seconds. If omitted, waits indefinitely."}
                    },
                    "required": ["agent_id"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delegate_task",
                "description": DELEGATE_TASK_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_prompt": {
                            "type": "string",
                            "description": "Detailed instructions for the sub-agent. Be specific about what to do and what to return.",
                        },
                        "attempts": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Maximum number of repeated failed attempts allowed before the sub-agent aborts. Default is 2.",
                        },
                        "background": {
                            "type": "boolean",
                            "description": "If true, launches the agent and returns immediately. If false, waits for the final report. Default is false.",
                        },
                    },
                    "required": ["task_prompt"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_open",
                "description": BROWSE_OPEN_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile": {"type": "string", "description": "'fresh' for clean browser, or 'Chrome'/'Brave'/'Edge' for real profile. Default 'fresh'."},
                        "url": {"type": "string", "description": "URL to navigate to after opening."},
                        "incognito": {"type": "boolean", "description": "Open in incognito mode. Default false."},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_close",
                "description": BROWSE_CLOSE_PROMPT,
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_navigate",
                "description": BROWSE_NAVIGATE_PROMPT,
                "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_screenshot",
                "description": BROWSE_SCREENSHOT_PROMPT,
                "parameters": {"type": "object", "properties": {"full_page": {"type": "boolean"}}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_click",
                "description": BROWSE_CLICK_PROMPT,
                "parameters": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}, "required": ["x", "y"], "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_type",
                "description": BROWSE_TYPE_PROMPT,
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "press_enter": {"type": "boolean"}}, "required": ["text"], "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_key",
                "description": BROWSE_KEY_PROMPT,
                "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"], "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_scroll",
                "description": BROWSE_SCROLL_PROMPT,
                "parameters": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down"]}, "amount": {"type": "integer"}}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_get_html",
                "description": BROWSE_GET_HTML_PROMPT,
                "parameters": {"type": "object", "properties": {"max_length": {"type": "integer"}}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_get_text",
                "description": BROWSE_GET_TEXT_PROMPT,
                "parameters": {"type": "object", "properties": {"max_length": {"type": "integer"}}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_back",
                "description": BROWSE_BACK_PROMPT,
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_forward",
                "description": BROWSE_FORWARD_PROMPT,
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_tab_new",
                "description": BROWSE_TAB_NEW_PROMPT,
                "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_tab_list",
                "description": BROWSE_TAB_LIST_PROMPT,
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_tab_switch",
                "description": BROWSE_TAB_SWITCH_PROMPT,
                "parameters": {"type": "object", "properties": {"index": {"type": "integer"}}, "required": ["index"], "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "clean_sot",
                "description": CLEAN_SOT_PROMPT,
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },

    ]


# ─── Schema builders extracted to keep get_tool_schemas readable ─────────


def _edit_files_schema() -> dict[str, Any]:
    """Build the ``edit_files`` tool schema.

    Lifted out of ``get_tool_schemas`` because the nested shape is deep
    (``files[]`` → ``edits[]`` → per-edit field grid) and inlining it
    pushed indentation past the point where misnested braces become hard
    to spot.
    """
    edit_item = {
        "type": "object",
        "description": (
            "Each edit picks EXACTLY ONE targeting mode by which keys it carries: "
            "text mode (old_string), line-range mode (start_line + end_line), or "
            "insert mode (insert_line + position). Mixing keys across modes is "
            "rejected."
        ),
        "properties": {
            "new_string": {
                "type": "string",
                "description": (
                    "Replacement text for replace/line-range modes, or the content "
                    "to insert in insert mode. Set to \"\" to delete the targeted "
                    "span (text or line-range only — insert mode rejects empty "
                    "new_string as a no-op)."
                ),
            },
            "old_string": {
                "type": "string",
                "description": (
                    "TEXT MODE. Exact text to replace. Must be unique in the file "
                    "unless replace_all=true or you supply before_context/"
                    "after_context to disambiguate. old_string=\"\" is ONLY valid "
                    "for creating a new file (single text-mode edit, file must not exist). "
                    "Rejected on existing files."
                ),
            },
            "before_context": {
                "type": "string",
                "description": (
                    "TEXT MODE ONLY. Exact text that must appear immediately "
                    "before old_string for a candidate to be considered a match — "
                    "use this to pick the right occurrence without enlarging "
                    "old_string. Invalid in line-range or insert mode."
                ),
            },
            "after_context": {
                "type": "string",
                "description": (
                    "TEXT MODE ONLY. Exact text that must appear immediately after "
                    "old_string for a candidate to be considered a match. Invalid in line-range or insert mode."
                ),
            },
            "replace_all": {
                "type": "boolean",
                "description": (
                    "TEXT MODE ONLY. When true, replace every occurrence of "
                    "old_string (after context filtering) instead of requiring a "
                    "single unique match. Invalid in line-range or insert mode."
                ),
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "LINE-RANGE MODE. REQUIRED together with end_line. "
                    "1-indexed first line of the block to replace or delete."
                ),
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "LINE-RANGE MODE. REQUIRED together with start_line. "
                    "1-indexed last line (inclusive) of the block to replace or delete. Must be >= start_line."
                ),
            },
            "insert_line": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "INSERT MODE. REQUIRED together with position. "
                    "1-indexed reference line. The insertion is zero-width and never modifies this line's contents."
                ),
            },
            "position": {
                "type": "string",
                "enum": ["before", "after"],
                "description": (
                    "INSERT MODE. REQUIRED together with insert_line. "
                    "\"before\" places new_string just before the line at insert_line; "
                    "\"after\" places it just after. Use insert_line=N, position=\"after\" "
                    "with N=last line to append at end of file."
                ),
            },
        },
        "required": ["new_string"],
        "additionalProperties": False,
    }

    file_item = {
        "type": "object",
        "description": "One file's edit batch. The 'edits' array is atomic for THIS file.",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Absolute path or project-relative path to the file. If the "
                    "file does not exist and this entry has exactly one text-mode "
                    "edit with old_string=\"\", the file is created."
                ),
            },
            "edits": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "One or more atomic edits applied to THIS file as a single "
                    "transaction. Resolved against the ORIGINAL content of THIS "
                    "file, then spliced in descending offset order, so line "
                    "numbers and anchors stay valid across edits. If any edit "
                    "fails (target not found, overlap, out-of-range line) NOTHING "
                    "in this file is written; other files in the same call are "
                    "unaffected. ALWAYS prefer batching multi-edit changes in "
                    "this array over splitting them across calls."
                ),
                "items": edit_item,
            },
        },
        "required": ["path", "edits"],
        "additionalProperties": False,
    }

    return {
        "type": "function",
        "function": {
            "name": "edit_files",
            "description": EDIT_FILES_PROMPT,
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "minItems": 1,
                        "description": (
                            "One or more files to edit in a single batched call. "
                            "Each entry is independent: a failure on one file does "
                            "NOT roll back another file's success. The response "
                            "carries a per-file results array. ALWAYS prefer "
                            "batching multi-file edits into one call over multiple "
                            "single-file calls — this is what this tool exists "
                            "for, exactly like read_files for the read side."
                        ),
                        "items": file_item,
                    },
                },
                "required": ["files"],
                "additionalProperties": False,
            },
        },
    }