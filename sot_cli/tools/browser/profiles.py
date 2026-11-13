"""
Detect installed Chromium-based browsers and their user profiles (macOS, Windows, Linux).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


# ── Browser definitions: (name, display_name, user_data_rel) ──
_BROWSER_DEFS: list[tuple[str, str, str]] = [
    ("Brave", "Brave Browser", "BraveSoftware/Brave-Browser"),
    ("Chrome", "Google Chrome", "Google/Chrome"),
    ("Edge", "Microsoft Edge", "Microsoft Edge"),
    ("Chromium", "Chromium", "Chromium"),
    ("Arc", "Arc", "Arc/User Data"),
]


def _default_exe(browser_name: str) -> Path | None:
    """Find the browser executable on macOS, Windows, or Linux."""
    system = sys.platform

    # ── macOS ──
    if system == "darwin":
        app_names = {
            "Brave": "Brave Browser.app",
            "Chrome": "Google Chrome.app",
            "Edge": "Microsoft Edge.app",
            "Chromium": "Chromium.app",
            "Arc": "Arc.app",
        }
        app_name = app_names.get(browser_name)
        if not app_name:
            return None
        macos_name = app_name.replace(".app", "")
        path = Path(f"/Applications/{app_name}/Contents/MacOS/{macos_name}")
        return path if path.exists() else None

    # ── Windows ──
    if system == "win32":
        # Try Program Files first, then LOCALAPPDATA
        program_files = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        local_app = Path(os.environ.get("LOCALAPPDATA", ""))

        win_exes = {
            "Brave": [
                program_files / "BraveSoftware/Brave-Browser/Application/brave.exe",
                local_app / "BraveSoftware/Brave-Browser/Application/brave.exe",
            ],
            "Chrome": [
                program_files / "Google/Chrome/Application/chrome.exe",
                program_files_x86 / "Google/Chrome/Application/chrome.exe",
                local_app / "Google/Chrome/Application/chrome.exe",
            ],
            "Edge": [
                program_files / "Microsoft/Edge/Application/msedge.exe",
                program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
            ],
            "Chromium": [
                program_files / "Chromium/Application/chrome.exe",
                local_app / "Chromium/Application/chrome.exe",
            ],
        }
        candidates = win_exes.get(browser_name, [])
        for path in candidates:
            if path.exists():
                return path
        return None

    # ── Linux ──
    linux_bins = {
        "Brave": ["/usr/bin/brave-browser", "/usr/bin/brave-browser-stable", "/snap/bin/brave"],
        "Chrome": ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/snap/bin/chromium"],
        "Edge": ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"],
        "Chromium": ["/usr/bin/chromium-browser", "/usr/bin/chromium"],
        "Arc": [],  # Arc not officially available on Linux
    }
    candidates = linux_bins.get(browser_name, [])
    for path_str in candidates:
        path = Path(path_str)
        if path.exists():
            return path

    # Linux fallback: try `which` for common names
    if system == "linux" or system.startswith("linux"):
        which_names = {
            "Brave": "brave-browser",
            "Chrome": "google-chrome",
            "Edge": "microsoft-edge",
            "Chromium": "chromium-browser",
        }
        cmd = which_names.get(browser_name)
        if cmd:
            try:
                result = subprocess.run(["which", cmd], capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    return Path(result.stdout.strip())
            except Exception:
                pass

    return None


def _default_user_data(browser_name: str) -> Path | None:
    """Find the browser user-data directory on macOS, Windows, or Linux."""
    system = sys.platform

    mapping = {
        "Brave": "BraveSoftware/Brave-Browser",
        "Chrome": "Google/Chrome",
        "Edge": "Microsoft Edge",
        "Chromium": "Chromium",
        "Arc": "Arc/User Data",
    }
    rel = mapping.get(browser_name)
    if not rel:
        return None

    # ── macOS ──
    if system == "darwin":
        path = Path.home() / "Library" / "Application Support" / rel
        return path if path.is_dir() else None

    # ── Windows ──
    if system == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        if not local:
            return None
        # Edge and Chrome use slightly different naming on Windows
        if browser_name == "Edge":
            path = local / "Microsoft" / "Edge" / "User Data"
        elif browser_name == "Chrome":
            path = local / "Google" / "Chrome" / "User Data"
        else:
            path = local / rel
        return path if path.is_dir() else None

    # ── Linux ──
    home = Path.home()
    linux_user_data = {
        "Brave": home / ".config" / "BraveSoftware" / "Brave-Browser",
        "Chrome": home / ".config" / "google-chrome",
        "Edge": home / ".config" / "microsoft-edge",
        "Chromium": home / ".config" / "chromium",
        "Arc": None,  # Arc not available on Linux
    }
    path = linux_user_data.get(browser_name)
    if path and path.is_dir():
        return path

    # Fallback: try standard ~/.config/<rel>
    fallback = home / ".config" / rel.replace("\\", "/")
    return fallback if fallback.is_dir() else None


def list_browser_profiles() -> list[dict[str, Any]]:
    """Return all detected browser profiles."""
    profiles: list[dict[str, Any]] = []

    for browser_name, _, _ in _BROWSER_DEFS:
        exe = _default_exe(browser_name)
        user_data = _default_user_data(browser_name)

        if not exe or not user_data:
            continue

        # ── Try to read profile list from Local State (Chrome/Edge/Brave/Chromium) ──
        local_state_path = user_data / "Local State"
        profile_dirs_found: list[str] = []
        if local_state_path.exists():
            try:
                with open(local_state_path, encoding="utf-8", errors="ignore") as f:
                    local_state = json.load(f)
                info_cache = local_state.get("profile", {}).get("info_cache", {})
                for directory, info in info_cache.items():
                    profile_dirs_found.append(directory)
                    display_name = info.get("name", directory)
                    profiles.append({
                        "browser": browser_name,
                        "name": display_name,
                        "exe": str(exe),
                        "user_data": str(user_data),
                        "profile_dir": directory,
                    })
            except (json.JSONDecodeError, OSError, KeyError):
                pass

        # ── Fallback: scan known profile directories ──
        if not profile_dirs_found:
            for profile_dir_name in ("Default", "Profile 1", "Profile 2", "Profile 3", "Profile 4", "Profile 5"):
                prefs_path = user_data / profile_dir_name / "Preferences"
                if not prefs_path.exists():
                    continue

                display_name = profile_dir_name
                try:
                    with open(prefs_path, encoding="utf-8", errors="ignore") as f:
                        prefs = json.load(f)
                    display_name = prefs.get("profile", {}).get("name", profile_dir_name)
                except (json.JSONDecodeError, OSError):
                    pass

                profiles.append({
                    "browser": browser_name,
                    "name": display_name,
                    "exe": str(exe),
                    "user_data": str(user_data),
                    "profile_dir": profile_dir_name,
                })
                profile_dirs_found.append(profile_dir_name)

        # ── Last resort: at least record the browser exists ──
        if not profile_dirs_found:
            profiles.append({
                "browser": browser_name,
                "name": "Default",
                "exe": str(exe),
                "user_data": str(user_data),
                "profile_dir": "Default",
            })

    return profiles
