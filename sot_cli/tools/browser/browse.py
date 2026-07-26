"""
Browser tools for sot-cli — Powered by browser-use 0.12.5 (Playwright-based).

sot-cli is the brain. browser-use is just the hands and eyes.
Uses Browser + BrowserConfig + BrowserContext + Playwright pages.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import sys
import threading
import traceback
from pathlib import Path
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


# Global state
_browser: Any = None  # browser_use.Browser (0.12.x)
_context: Any = None  # browser_use.BrowserContext


async def _get_page():
    """Get the current Playwright Page from the BrowserContext."""
    if not _context:
        return None
    try:
        return await _context.get_current_page()
    except Exception:
        return None


# ──────────────────────────────────────────────────────
# browser_open
# ──────────────────────────────────────────────────────
def execute_browser_open(arguments: dict[str, Any]) -> dict[str, Any]:
    profile = arguments.get("profile", "fresh")
    url = arguments.get("url", "")
    logger.info(f"browser_open called: profile={profile!r} url={url!r}")

    async def _open():
        global _browser, _context

        # Close existing context/browser
        if _context is not None:
            try:
                await _context.close()
            except Exception:
                pass
            _context = None
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None

        from browser_use import Browser, BrowserConfig

        extra_args: list[str] = []

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

            chrome_path = matched["exe"]
            user_data = matched["user_data"]
            profile_dir = matched["profile_dir"]

            logger.info(
                f"Launching {matched['browser']}/{profile_dir} from {chrome_path}"
            )

            extra_args.append(f"--user-data-dir={user_data}")
            extra_args.append(f"--profile-directory={profile_dir}")

            config = BrowserConfig(
                headless=False,
                browser_binary_path=chrome_path,
                extra_browser_args=extra_args,
            )
        else:
            config = BrowserConfig(
                headless=False,
            )

        _browser = Browser(config=config)

        # browser-use 0.12.5: launch the process + create a Playwright context
        pw = await _browser.get_playwright_browser()
        _context = await _browser.new_context()

        page = await _get_page()
        if page is None:
            raise RuntimeError("Browser opened but could not get a page.")

        # Navigation — best-effort (Brave shields, etc. may throw)
        if url:
            try:
                await page.goto(url)
                await page.wait_for_load_state()
            except Exception:
                pass

        title = await page.title()
        page_url = page.url
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
        page = await _get_page()
        if not page:
            return None
        await page.goto(url)
        await page.wait_for_load_state()
        title = await page.title()
        page_url = page.url
        return {"title": title, "url": page_url}

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
        page = await _get_page()
        if not page:
            return None
        screenshot_bytes = await page.screenshot(full_page=full_page, animations="disabled")
        path = "/tmp/sot_browser_screenshot.png"
        with open(path, "wb") as f:
            f.write(screenshot_bytes)
        title = await page.title()
        page_url = page.url
        logger.info(f"Screenshot saved: {path} ({len(screenshot_bytes)} bytes)")
        return {
            "title": title,
            "url": page_url,
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
        await page.mouse.click(x, y)
        await asyncio.sleep(0.5)
        title = await page.title()
        page_url = page.url
        return {
            "title": title,
            "url": page_url,
            "clicked": {"x": x, "y": y},
        }

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
        # Playwright keyboard.type with 50ms delay between keystrokes
        await page.keyboard.type(text, delay=50)
        if press_enter:
            await page.keyboard.press("Enter")
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
        await page.keyboard.press(key)
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
        # Playwright mouse.wheel(delta_y=...)
        await page.mouse.wheel(delta_y=delta)
        await asyncio.sleep(0.3)
        title = await page.title()
        page_url = page.url
        return {
            "title": title,
            "url": page_url,
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
        title = await page.title()
        page_url = page.url
        return {
            "title": title,
            "url": page_url,
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

        title = await page.title()
        page_url = page.url
        return {
            "title": title,
            "url": page_url,
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
        title = await page.title()
        page_url = page.url
        return {"title": title, "url": page_url}

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
        title = await page.title()
        page_url = page.url
        return {"title": title, "url": page_url}

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
        if not _context:
            return None
        await _context.create_new_tab(url=url if url else None)
        page = await _get_page()
        title = await page.title()
        page_url = page.url
        return {"title": title, "url": page_url}

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
        if not _context:
            return []
        tabs_info = await _context.get_tabs_info()
        tabs = []
        for tab in tabs_info:
            tabs.append({
                "index": tab.page_id,
                "title": tab.title,
                "url": tab.url,
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
        if not _context:
            return None
        await _context.switch_to_tab(page_id=index)
        page = await _get_page()
        title = await page.title()
        page_url = page.url
        return {"title": title, "url": page_url}

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
        global _browser, _context
        if _context:
            try:
                await _context.close()
            except Exception:
                pass
            _context = None
        if _browser:
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None

    try:
        loop = _ensure_loop()
        future = asyncio.run_coroutine_threadsafe(_close(), loop)
        future.result(timeout=15)
        return {"ok": True, "message": "Browser closed."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
