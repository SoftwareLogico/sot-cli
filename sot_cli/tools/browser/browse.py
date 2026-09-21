"""
Browser tools for sot-cli — Powered by browser-use 0.12.5 (CDP-based).

sot-cli is the brain. browser-use is just the hands and eyes.
Uses BrowserSession + BrowserProfile + actor Page (pure CDP — no Playwright).

Migration notes (browser-use 0.11.x → 0.12.5):
- Browser/BrowserConfig        → BrowserSession/BrowserProfile
- browser.get_playwright_browser() + browser.new_context() → session.start()
- context.get_current_page()   → session.get_current_page() (actor Page, CDP)
- page.title()/page.url        → session.get_current_page_title()/get_current_page_url()
- page.mouse.click/wheel       → (await page.mouse).click/scroll
- page.keyboard.type/press     → CDP Input.dispatchKeyEvent / page.press
- context.create_new_tab()     → session.navigate_to(url, new_tab=True)
- context.get_tabs_info()      → session.get_tabs() → list[TabInfo]
- context.switch_to_tab()      → event_bus.dispatch(SwitchTabEvent(target_id=...))
- browser/context.close()      → session.kill()
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from typing import Any

logger = logging.getLogger("sot.browser")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.DEBUG)

# ── Dedicated event loop thread ──
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_ready = threading.Event()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    if _loop is not None and not _loop.is_closed():
        return _loop
    _loop_ready.clear()

    def _run_loop():
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _loop_ready.set()
        _loop.run_forever()

    _loop_thread = threading.Thread(target=_run_loop, daemon=True, name="browser-loop")
    _loop_thread.start()
    _loop_ready.wait(timeout=10)
    if _loop is None:
        raise RuntimeError("Failed to start browser event loop")
    return _loop


def _run_async(coro):
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=120)


def _kill_stale_instances(exe_path: str) -> int:
    """Kill any running instance of the target browser executable before launching.

    Chromium browsers allow a single instance per user-data-dir (SingletonLock).
    A stale instance (left open by a previous or aborted session) silently swallows
    the launch command line: the new process exits immediately without printing the
    "DevTools listening on ws://..." line, so browser-use's on_BrowserLaunchEvent
    handler hangs until the 30s event-bus timeout and browser_open fails — leaving
    windows open that no session can control. Killing stale instances first keeps
    the launch handshake deterministic.
    """
    if sys.platform == "win32":
        name = os.path.basename(exe_path)
        try:
            probe = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            return 0
        if probe.returncode != 0 or name.lower() not in probe.stdout.lower():
            return 0
        logger.info(f"Killing stale {name} instance(s) before launch")
        subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True, timeout=10)
        time.sleep(1.0)
        return 1

    try:
        probe = subprocess.run(
            ["pgrep", "-f", exe_path], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return 0
    pids = [ln.strip() for ln in probe.stdout.splitlines() if ln.strip()]
    if not pids:
        return 0

    logger.info(f"Killing {len(pids)} stale browser instance(s): {', '.join(pids)}")
    subprocess.run(["pkill", "-f", exe_path], capture_output=True, timeout=5)
    for _ in range(10):
        check = subprocess.run(
            ["pgrep", "-f", exe_path], capture_output=True, text=True, timeout=5
        )
        if not check.stdout.strip():
            break
        time.sleep(0.5)
    else:
        logger.warning("Stale instances survived SIGTERM; sending SIGKILL")
        subprocess.run(["pkill", "-9", "-f", exe_path], capture_output=True, timeout=5)
        time.sleep(0.5)
    return len(pids)


# Global state
_session: Any = None  # browser_use.BrowserSession (0.12.5)


async def _get_page():
    """Get the current browser_use actor Page (CDP) from the session."""
    if not _session:
        return None
    try:
        return await _session.get_current_page()
    except Exception:
        return None


async def _page_meta() -> dict[str, str]:
    """Fast title/url from the session's cached target info (no CDP roundtrip)."""
    if not _session:
        return {"title": "", "url": ""}
    try:
        title = await _session.get_current_page_title()
    except Exception:
        title = ""
    try:
        url = await _session.get_current_page_url()
    except Exception:
        url = ""
    return {"title": title, "url": url}


async def _dispatch_event(event) -> None:
    """Dispatch a browser_use event on the session bus and propagate handler errors."""
    ev = _session.event_bus.dispatch(event)
    await ev
    await ev.event_result(raise_if_any=True, raise_if_none=False)


async def _enforce_window(window_size: dict[str, int], window_position: dict[str, int] | None) -> None:
    """Force the browser window into a normal (not maximized/minimized) state with the
    requested size via CDP Browser.setWindowBounds.

    macOS/Brave session restore can override the --window-size CLI flag on launch;
    this re-asserts the geometry after the session is up, so the window always ends
    up normal-sized regardless of restored state.
    """
    if not _session:
        return
    try:
        cdp = await _session.get_or_create_cdp_session()
        result = await cdp.cdp_client.send_raw(
            "Browser.getWindowForTarget", {}, session_id=cdp.session_id
        )
        window_id = result["windowId"]
        bounds: dict[str, Any] = {
            "windowState": "normal",
            "width": int(window_size["width"]),
            "height": int(window_size["height"]),
        }
        if window_position:
            bounds["left"] = int(window_position["width"])  # CDP: left = x
            bounds["top"] = int(window_position["height"])  # CDP: top = y
        await cdp.cdp_client.send_raw(
            "Browser.setWindowBounds",
            {"windowId": window_id, "bounds": bounds},
            session_id=cdp.session_id,
        )
        logger.info(f"Window bounds set: {bounds}")
    except Exception as exc:
        logger.debug(f"Window bounds enforcement skipped: {exc}")


# ──────────────────────────────────────────────────────
# browser_open
# ──────────────────────────────────────────────────────
def execute_browser_open(arguments: dict[str, Any]) -> dict[str, Any]:
    profile = arguments.get("profile", "fresh")
    url = arguments.get("url", "")
    logger.info(f"browser_open called: profile={profile!r} url={url!r}")

    # Window geometry: open a normal windowed browser (not maximized/minimized).
    # browser-use emits --start-maximized by default when window_size is unset;
    # passing window_size emits --window-size=W,H instead.
    window_size = arguments.get("window_size") or {"width": 1280, "height": 720}
    window_position = arguments.get("window_position")  # optional {"width": x, "height": y}
    window_kwargs: dict[str, Any] = {"window_size": window_size}
    if window_position:
        window_kwargs["window_position"] = window_position

    async def _open():
        global _session

        # Close existing session (kills browser process + resets state)
        if _session is not None:
            try:
                await _session.kill()
            except Exception:
                pass
            _session = None

        from browser_use import BrowserProfile, BrowserSession

        if profile != "fresh":
            from sot_cli.tools.browser.profiles import list_browser_profiles

            profiles = list_browser_profiles()
            matched = next(
                (p for p in profiles if p["browser"].lower() == profile.lower()),
                None,
            )

            if not matched:
                raise RuntimeError(
                    f"Profile '{profile}' not found. Available: {[p['browser'] for p in profiles]}"
                )

            logger.info(
                f"Launching {matched['browser']}/{matched['profile_dir']} from {matched['exe']}"
            )

            # Chromium single-instance rule: a stale instance blocks the CDP
            # launch handshake (see _kill_stale_instances docstring).
            _kill_stale_instances(matched["exe"])

            # Real user profile: browser-use passes --user-data-dir and
            # --profile-directory itself (get_args()). For Brave it does NOT
            # copy the profile to a temp dir (that only happens for Chrome),
            # so cookies/logins are real and persistent.
            browser_profile = BrowserProfile(
                headless=False,
                executable_path=matched["exe"],
                user_data_dir=matched["user_data"],
                profile_directory=matched["profile_dir"],
                **window_kwargs,
            )
        else:
            browser_profile = BrowserProfile(headless=False, **window_kwargs)

        _session = BrowserSession(browser_profile=browser_profile)
        await _session.start()

        # Re-assert window geometry: macOS/Brave session restore can override
        # the --window-size CLI flag; CDP setWindowBounds is the final word.
        await _enforce_window(window_size, window_position)

        # Ensure a tab exists; navigate if a URL was requested.
        page = await _get_page()
        if page is None:
            await _session.navigate_to(url or "about:blank")
        elif url:
            await _session.navigate_to(url)

        title = await _session.get_current_page_title()
        page_url = await _session.get_current_page_url()
        return {"title": title, "url": page_url}

    try:
        result = _run_async(_open())
        return {"ok": True, **result}
    except Exception as exc:
        logger.error(f"browser_open failed: {exc}\n{traceback.format_exc()}")
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_navigate
# ──────────────────────────────────────────────────────
def execute_browser_navigate(arguments: dict[str, Any]) -> dict[str, Any]:
    url = arguments.get("url", "").strip()
    if not url:
        return {"ok": False, "error": "url is required"}

    async def _nav():
        if not _session:
            return None
        # navigate_to() handles tab reuse/creation and waits for page readiness.
        await _session.navigate_to(url)
        return await _page_meta()

    try:
        result = _run_async(_nav())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open. Use browser_open first."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_screenshot
# ──────────────────────────────────────────────────────
def execute_browser_screenshot(arguments: dict[str, Any]) -> dict[str, Any]:
    full_page = arguments.get("full_page", False)

    async def _screenshot():
        if not _session:
            return None
        screenshot_bytes = await _session.take_screenshot(full_page=full_page)
        path = "/tmp/sot_browser_screenshot.png"
        with open(path, "wb") as f:
            f.write(screenshot_bytes)
        logger.info(f"Screenshot saved: {path} ({len(screenshot_bytes)} bytes)")
        return {
            **await _page_meta(),
            "screenshot_path": path,
            "size_bytes": len(screenshot_bytes),
        }

    try:
        result = _run_async(_screenshot())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_click
# ──────────────────────────────────────────────────────
def execute_browser_click(arguments: dict[str, Any]) -> dict[str, Any]:
    x = arguments.get("x")
    y = arguments.get("y")
    if x is None or y is None:
        return {"ok": False, "error": "x and y are required"}

    async def _click():
        page = await _get_page()
        if not page:
            return None
        mouse = await page.mouse
        await mouse.move(x, y)  # hover state before pressing
        await mouse.click(x, y)
        await asyncio.sleep(0.5)
        return {**await _page_meta(), "clicked": {"x": x, "y": y}}

    try:
        result = _run_async(_click())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_type
# ──────────────────────────────────────────────────────
def execute_browser_type(arguments: dict[str, Any]) -> dict[str, Any]:
    text = arguments.get("text", "")
    press_enter = arguments.get("press_enter", False)
    if not text:
        return {"ok": False, "error": "text is required"}

    async def _type():
        page = await _get_page()
        if not page:
            return None
        # Real per-character key events via CDP (equivalent to Playwright's
        # keyboard.type with delay): keyDown(text=ch) + keyUp per character.
        cdp = await _session.get_or_create_cdp_session()
        for ch in text:
            await cdp.cdp_client.send.Input.dispatchKeyEvent(
                {"type": "keyDown", "key": ch, "text": ch, "unmodifiedText": ch},
                session_id=cdp.session_id,
            )
            await cdp.cdp_client.send.Input.dispatchKeyEvent(
                {"type": "keyUp", "key": ch},
                session_id=cdp.session_id,
            )
            await asyncio.sleep(0.05)  # 50ms between keystrokes
        if press_enter:
            await page.press("Enter")
            await asyncio.sleep(0.5)
        return {"typed": text, "pressed_enter": press_enter}

    try:
        result = _run_async(_type())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_key
# ──────────────────────────────────────────────────────
def execute_browser_key(arguments: dict[str, Any]) -> dict[str, Any]:
    key = arguments.get("key", "")
    if not key:
        return {"ok": False, "error": "key is required"}

    async def _key():
        page = await _get_page()
        if not page:
            return None
        await page.press(key)
        return {"pressed": key}

    try:
        result = _run_async(_key())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_scroll
# ──────────────────────────────────────────────────────
def execute_browser_scroll(arguments: dict[str, Any]) -> dict[str, Any]:
    direction = arguments.get("direction", "down")
    amount = arguments.get("amount", 500)

    async def _scroll():
        page = await _get_page()
        if not page:
            return None
        delta = amount if direction == "down" else -amount
        mouse = await page.mouse
        # CDP mouse wheel with synthesizeScrollGesture + JS fallbacks built in
        await mouse.scroll(delta_y=delta)
        await asyncio.sleep(0.3)
        return {
            **await _page_meta(),
            "scrolled": {"direction": direction, "amount": amount},
        }

    try:
        result = _run_async(_scroll())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_get_html
# ──────────────────────────────────────────────────────
def execute_browser_get_html(arguments: dict[str, Any]) -> dict[str, Any]:
    max_length = arguments.get("max_length", 5000)

    async def _html():
        page = await _get_page()
        if not page:
            return None
        content = await page.evaluate("() => document.documentElement.outerHTML")
        truncated = content[:max_length] if len(content) > max_length else content
        return {
            **await _page_meta(),
            "html": truncated,
            "total_length": len(content),
        }

    try:
        result = _run_async(_html())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_get_text
# ──────────────────────────────────────────────────────
def execute_browser_get_text(arguments: dict[str, Any]) -> dict[str, Any]:
    max_length = arguments.get("max_length", 50000)

    async def _text():
        page = await _get_page()
        if not page:
            return None
        content = await page.evaluate("() => document.body.innerText")

        # Save full text to file
        text_path = "/tmp/sot_browser_get_text.txt"
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(content)

        return {
            **await _page_meta(),
            "text_file_path": text_path,
            "total_length": len(content),
        }

    try:
        result = _run_async(_text())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_back
# ──────────────────────────────────────────────────────
def execute_browser_back(arguments: dict[str, Any]) -> dict[str, Any]:
    async def _back():
        page = await _get_page()
        if not page:
            return None
        await page.go_back()
        await asyncio.sleep(0.5)
        return await _page_meta()

    try:
        result = _run_async(_back())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_forward
# ──────────────────────────────────────────────────────
def execute_browser_forward(arguments: dict[str, Any]) -> dict[str, Any]:
    async def _forward():
        page = await _get_page()
        if not page:
            return None
        await page.go_forward()
        await asyncio.sleep(0.5)
        return await _page_meta()

    try:
        result = _run_async(_forward())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_tab_new
# ──────────────────────────────────────────────────────
def execute_browser_tab_new(arguments: dict[str, Any]) -> dict[str, Any]:
    url = arguments.get("url", "")

    async def _new_tab():
        if not _session:
            return None
        if url:
            # Creates a new tab, navigates it and switches focus to it.
            await _session.navigate_to(url, new_tab=True)
        else:
            # SwitchTabEvent(target_id=None) creates a blank tab and focuses it.
            from browser_use.browser.events import SwitchTabEvent

            await _dispatch_event(SwitchTabEvent(target_id=None))
        return await _page_meta()

    try:
        result = _run_async(_new_tab())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_tab_list
# ──────────────────────────────────────────────────────
def execute_browser_tab_list(arguments: dict[str, Any]) -> dict[str, Any]:
    async def _list_tabs():
        if not _session:
            return []
        tabs_info = await _session.get_tabs()
        tabs = []
        for i, tab in enumerate(tabs_info):
            tabs.append({
                "index": i,
                "title": tab.title,
                "url": tab.url,
                "target_id": tab.target_id,
            })
        return tabs

    try:
        tabs = _run_async(_list_tabs())
        return {"ok": True, "tabs": tabs}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_tab_switch
# ──────────────────────────────────────────────────────
def execute_browser_tab_switch(arguments: dict[str, Any]) -> dict[str, Any]:
    index = arguments.get("index", 0)

    async def _switch():
        if not _session:
            return None
        from browser_use.browser.events import SwitchTabEvent

        tabs_info = await _session.get_tabs()
        if index < 0 or index >= len(tabs_info):
            raise IndexError(f"Tab index {index} not found (0..{len(tabs_info) - 1}).")
        await _dispatch_event(SwitchTabEvent(target_id=tabs_info[index].target_id))
        return await _page_meta()

    try:
        result = _run_async(_switch())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": f"Tab index {index} not found."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ──────────────────────────────────────────────────────
# browser_close
# ──────────────────────────────────────────────────────
def execute_browser_close(arguments: dict[str, Any]) -> dict[str, Any]:
    logger.info("browser_close called")

    async def _close():
        global _session
        if _session:
            try:
                await _session.kill()
            except Exception:
                pass
            _session = None

    try:
        loop = _ensure_loop()
        future = asyncio.run_coroutine_threadsafe(_close(), loop)
        future.result(timeout=30)
        return {"ok": True, "message": "Browser closed."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
