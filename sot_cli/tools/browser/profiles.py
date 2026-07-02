"""
Detect installed Chromium-based browsers and their user profiles on macOS.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_BROWSER_DEFS: list[tuple[str, str, str]] = [
    ("Brave", "Brave Browser", "BraveSoftware/Brave-Browser"),
    ("Chrome", "Google Chrome", "Google/Chrome"),
    ("Edge", "Microsoft Edge", "Microsoft Edge"),
    ("Chromium", "Chromium", "Chromium"),
    ("Arc", "Arc", "Arc/User Data"),
]


def _default_exe(browser_name: str) -> Path | None:
    app_name = {
        "Brave": "Brave Browser.app",
        "Chrome": "Google Chrome.app",
        "Edge": "Microsoft Edge.app",
        "Chromium": "Chromium.app",
        "Arc": "Arc.app",
    }.get(browser_name)
    if not app_name:
        return None
    path = Path(f"/Applications/{app_name}/Contents/MacOS/{app_name.replace('.app', '')}")
    return path if path.exists() else None


def _default_user_data(browser_name: str) -> Path | None:
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
    path = Path.home() / "Library" / "Application Support" / rel
    return path if path.is_dir() else None


def list_browser_profiles() -> list[dict[str, Any]]:
    """Return all detected browser profiles."""
    profiles: list[dict[str, Any]] = []

    for browser_name, _, _ in _BROWSER_DEFS:
        exe = _default_exe(browser_name)
        user_data = _default_user_data(browser_name)

        if not exe or not user_data:
            continue

        found = False
        for profile_dir_name in ("Default", "Profile 1", "Profile 2", "Profile 3", "Profile 4", "Profile 5"):
            prefs_path = user_data / profile_dir_name / "Preferences"
            if not prefs_path.exists():
                continue

            display_name = "Default"
            try:
                with open(prefs_path, encoding="utf-8", errors="ignore") as f:
                    prefs = json.load(f)
                display_name = prefs.get("profile", {}).get("name", profile_dir_name)
            except (json.JSONDecodeError, OSError):
                display_name = profile_dir_name

            profiles.append({
                "browser": browser_name,
                "name": display_name,
                "exe": str(exe),
                "user_data": str(user_data),
                "profile_dir": profile_dir_name,
            })
            found = True

        if not found:
            profiles.append({
                "browser": browser_name,
                "name": "Default",
                "exe": str(exe),
                "user_data": str(user_data),
                "profile_dir": "Default",
            })

    return profiles
