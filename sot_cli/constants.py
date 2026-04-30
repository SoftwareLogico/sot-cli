from __future__ import annotations

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "tiff", "tif"}
PDF_EXTENSIONS = {"pdf"}
NOTEBOOK_EXTENSIONS = {"ipynb"}
AUDIO_EXTENSIONS = {"mp3", "wav", "ogg", "flac", "aac", "m4a", "wma", "aiff", "opus"}
VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm", "wmv", "flv", "m4v", "mpeg", "mpg"}

# Binary extensions that this tool refuses to read.
# Images, PDFs, notebooks, audio, and video are excluded because they have dedicated handlers.
BINARY_EXTENSIONS = {
    "zip", "tar", "gz", "bz2", "7z", "rar", "xz", "z", "tgz", "iso",
    "exe", "dll", "so", "dylib", "bin", "o", "a", "obj", "lib", "app", "msi", "deb", "rpm",
    "pyc", "pyo", "class", "jar", "war",
    "ear", "node", "wasm", "rlib",
    "dmg", "img",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
    "woff", "woff2", "ttf", "otf", "eot",
    "sqlite", "sqlite3", "db", "mdb", "idx",
    "psd", "ai", "eps", "sketch", "fig", "xd", "blend", "3ds", "max",
    "swf", "fla",
    "lockb", "dat", "data",
    "DS_Store",
}

ARCHIVE_EXTENSIONS = {"zip", "tar", "gz", "bz2", "7z", "rar", "xz", "z", "tgz"}

ARCHIVE_HINTS: dict[str, str] = {
    "zip": (
        "ZIP archives may require a password to inspect their contents. "
        "Use run_command with: unzip -l {path} (list contents), "
        "unzip -P <password> {path} -d /tmp/out (extract with password), "
        "or zipinfo {path} for metadata."
    ),
    "7z": (
        "7z archives may require a password. "
        "Use run_command with: 7z l {path} (list), 7z x -p<password> {path} (extract with password)."
    ),
    "rar": (
        "RAR archives may require a password. "
        "Use run_command with: unrar l {path} (list), unrar x -p<password> {path} (extract with password)."
    ),
    "tar": "Use run_command with: tar -tf {path} (list contents), tar -xf {path} -C /tmp/out (extract).",
    "gz": "Use run_command with: gunzip -c {path} | head or zcat {path}.",
    "tgz": "Use run_command with: tar -tzf {path} (list), tar -xzf {path} -C /tmp/out (extract).",
    "bz2": "Use run_command with: tar -tjf {path} (list), bunzip2 -c {path} | head.",
    "xz": "Use run_command with: tar -tJf {path} (list), xz -d -c {path} | head.",
}

# Device paths that would hang the process (infinite output or blocking input).
BLOCKED_DEVICE_PATHS = {
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/tty", "/dev/console",
    "/dev/stdout", "/dev/stderr",
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
}

# ── Source of Truth ──
SOT_MARKER = "=== SOURCE OF TRUTH ==="

# ── Version control directories to exclude from searches and scans ──
VCS_DIRS = {".git", ".svn", ".hg", ".bzr", ".jj", ".sl"}

# ── Tool-config fallbacks (authoritative values live in [tools] of sot.toml) ──
# Used only when the runtime calls a tool without a config-backed override
# (e.g. direct library usage, tests, or legacy entry points).
FALLBACK_MAX_ROUNDS = 25
FALLBACK_DELEGATED_MAX_ROUNDS = 8
FALLBACK_REPEAT_LIMIT = 3
FALLBACK_DELEGATED_REPEAT_LIMIT = 2
FALLBACK_SEARCH_DEFAULT_HEAD_LIMIT = 200
FALLBACK_SEARCH_MAX_LINE_LENGTH = 500
FALLBACK_SEARCH_TIMEOUT_SECONDS = 30
# Hard cap on streamed reasoning/thinking characters per turn.
# If the model's reasoning channel exceeds this budget without emitting
# a final answer or tool call, the stream is cut and the round advances.
# Set to 0 to disable the cap.
FALLBACK_REASONING_CHAR_BUDGET = 0
FALLBACK_DELEGATED_REASONING_CHAR_BUDGET = 0
# Hard cap on characters kept from the `reasoning` field of any tool-bearing
# assistant message in OLD (already-closed) turns when the outbound payload
# is built. Keeps the narrative ("the model used edit_files because…") while
# discarding the long re-explanation of the file body the model often dumps
# inside its thinking right before emitting a mutation tool_call. Applied
# only to assistants that have at least one tool_call AND that are not the
# very last message in the active turn. Set to 0 to disable the cap (full
# reasoning round-trips for every turn).
FALLBACK_COMPRESSION_REASONING_TRUNC_CHARS = 240

# ── Tools that mutate the session (trigger SoT/session refresh) ──
SESSION_MUTATION_TOOLS = {
    "update_session",
    "attach_path_to_source",
    "detach_path_from_source",
}

# ── Tools whose tool_call ↔ tool_response pair gets fully compressed in
# OLD turns. The pair is replaced with a single `user` message of the form
# "SYSTEM MESSAGE: used tools: <tool> path=... ..." that conveys what the assistant
# called and the result, while the heavy `arguments` body (the full
# `new_string` blocks for edit_files, the full `content` for write_file)
# is permanently dropped from the wire payload. Compression only happens
# when the corresponding tool_response reports success — failed mutations
# are kept intact so the model can see the error context and not repeat it.
COMPRESSED_TOOLS: frozenset[str] = frozenset({"write_file", "edit_files"})