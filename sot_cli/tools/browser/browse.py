"""
Browser tools for sot-cli — Powered by browser-use 0.1.48 (Playwright-based).

sot-cli is the brain. browser-use is just the hands and eyes.
Uses Playwright directly via browser-use's get_playwright_browser().
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
_browser_instance = None  # browser_use.Browser
_playwright_browser = None  # playwright.async_api.Browser


async def _get_page():
    """Get the active Playwright Page from the browser."""
    global _playwright_browser
    if not _playwright_browser:
        return None
    contexts = _playwright_browser.contexts
    if not contexts:
        context = await _playwright_browser.new_context()
        return await context.new_page()
    pages = contexts[0].pages
    if not pages:
        return await contexts[0].new_page()
    return pages[0]


def execute_browser_open(arguments: dict[str, Any]) -> dict[str, Any]:
    profile = arguments.get("profile", "fresh")
    url = arguments.get("url", "")
    logger.info(f"browser_open called: profile={profile!r} url={url!r}")

    async def _open():
        global _browser_instance, _playwright_browser

        # Close existing browser
        if _browser_instance is not None:
            try:
                await _browser_instance.close()
            except Exception:
                pass
            _browser_instance = None
            _playwright_browser = None

        from browser_use import Browser
        from browser_use.browser.browser import BrowserConfig

        if profile == "fresh":
            config = BrowserConfig(headless=False)
        else:
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
                f"Launching Chrome: {matched['browser']}/{profile_dir} from {chrome_path}"
            )

            config = BrowserConfig(
                headless=False,
                browser_binary_path=chrome_path,
                extra_browser_args=[
                    f"--user-data-dir={user_data}",
                    f"--profile-directory={profile_dir}",
                ],
            )

        _browser_instance = Browser(config=config)
        _playwright_browser = await _browser_instance.get_playwright_browser()

        page = await _get_page()
        if page is None:
            raise RuntimeError("Browser opened but could not get a page.")

        # Navigation is best-effort — some browsers (Brave shields, etc.)
        # intercept redirects and throw ERR_ABORTED. Let the LLM handle
        # navigation separately via browser_navigate.
        if url:
            try:
                await page.goto(url, wait_until="commit", timeout=30000)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
            except Exception:
                # goto failed (Brave shields, network issue, redirect abort)
                # Just return whatever state the page is in.
                pass

        title = await page.title()
        return {"title": title, "url": page.url}

    try:
        result = _run_async(_open())
        return {"ok": True, **result}
    except Exception as exc:
        logger.error(f"browser_open failed: {exc}\n{traceback.format_exc()}")
        return {"ok": False, "error": str(exc)}


def execute_browser_navigate(arguments: dict[str, Any]) -> dict[str, Any]:
    url = arguments.get("url", "").strip()
    if not url:
        return {"ok": False, "error": "url is required"}

    async def _nav():
        page = await _get_page()
        if not page:
            return None
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {"title": await page.title(), "url": page.url}

    try:
        result = _run_async(_nav())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open. Use browser_open first."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def execute_browser_screenshot(arguments: dict[str, Any]) -> dict[str, Any]:
    full_page = arguments.get("full_page", False)

    async def _screenshot():
        page = await _get_page()
        if not page:
            return None
        img_bytes = await page.screenshot(full_page=full_page)
        # Save to temp file instead of returning base64 inline
        import os
        path = "/tmp/sot_browser_screenshot.png"
        with open(path, "wb") as f:
            f.write(img_bytes)
        title = await page.title()
        url = page.url
        logger.info(f"Screenshot saved: {path} ({len(img_bytes)} bytes)")
        return {
            "title": title,
            "url": url,
            "screenshot_path": path,
            "size_bytes": len(img_bytes),
        }

    try:
        result = _run_async(_screenshot())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


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
        return {
            "title": await page.title(),
            "url": page.url,
            "clicked": {"x": x, "y": y},
        }

    try:
        result = _run_async(_click())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def execute_browser_type(arguments: dict[str, Any]) -> dict[str, Any]:
    text = arguments.get("text", "")
    press_enter = arguments.get("press_enter", False)
    if not text:
        return {"ok": False, "error": "text is required"}

    async def _type():
        page = await _get_page()
        if not page:
            return None
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


def execute_browser_scroll(arguments: dict[str, Any]) -> dict[str, Any]:
    direction = arguments.get("direction", "down")
    amount = arguments.get("amount", 500)
    delta = amount if direction == "down" else -amount

    async def _scroll():
        page = await _get_page()
        if not page:
            return None
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(0.3)
        return {
            "title": await page.title(),
            "url": page.url,
            "scrolled": {"direction": direction, "amount": amount},
        }

    try:
        result = _run_async(_scroll())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def execute_browser_get_html(arguments: dict[str, Any]) -> dict[str, Any]:
    max_length = arguments.get("max_length", 5000)

    async def _html():
        page = await _get_page()
        if not page:
            return None
        content = await page.content()
        truncated = content[:max_length] if len(content) > max_length else content
        return {
            "title": await page.title(),
            "url": page.url,
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


def execute_browser_get_text(arguments: dict[str, Any]) -> dict[str, Any]:
    max_length = arguments.get("max_length", 5000)

    async def _text():
        page = await _get_page()
        if not page:
            return None
        content = await page.evaluate("() => document.body.innerText")
        truncated = content[:max_length] if len(content) > max_length else content
        return {
            "title": await page.title(),
            "url": page.url,
            "text": truncated,
            "total_length": len(content),
        }

    try:
        result = _run_async(_text())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def execute_browser_back(arguments: dict[str, Any]) -> dict[str, Any]:
    async def _back():
        page = await _get_page()
        if not page:
            return None
        await page.go_back(wait_until="domcontentloaded", timeout=15000)
        return {"title": await page.title(), "url": page.url}

    try:
        result = _run_async(_back())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def execute_browser_forward(arguments: dict[str, Any]) -> dict[str, Any]:
    async def _forward():
        page = await _get_page()
        if not page:
            return None
        await page.go_forward(wait_until="domcontentloaded", timeout=15000)
        return {"title": await page.title(), "url": page.url}

    try:
        result = _run_async(_forward())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def execute_browser_tab_new(arguments: dict[str, Any]) -> dict[str, Any]:
    url = arguments.get("url", "")

    async def _new_tab():
        global _playwright_browser
        if not _playwright_browser:
            return None
        contexts = _playwright_browser.contexts
        if not contexts:
            return None
        page = await contexts[0].new_page()
        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {"title": await page.title(), "url": page.url}

    try:
        result = _run_async(_new_tab())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def execute_browser_tab_list(arguments: dict[str, Any]) -> dict[str, Any]:
    async def _list_tabs():
        global _playwright_browser
        if not _playwright_browser:
            return []
        contexts = _playwright_browser.contexts
        if not contexts:
            return []
        tabs = []
        for i, page in enumerate(contexts[0].pages):
            tabs.append({"index": i, "title": await page.title(), "url": page.url})
        return tabs

    try:
        tabs = _run_async(_list_tabs())
        return {"ok": True, "tabs": tabs}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def execute_browser_tab_switch(arguments: dict[str, Any]) -> dict[str, Any]:
    index = arguments.get("index", 0)

    async def _switch():
        global _playwright_browser
        if not _playwright_browser:
            return None
        contexts = _playwright_browser.contexts
        if not contexts:
            return None
        pages = contexts[0].pages
        if 0 <= index < len(pages):
            page = pages[index]
            await page.bring_to_front()
            return {"title": await page.title(), "url": page.url}
        return None

    try:
        result = _run_async(_switch())
        if result:
            return {"ok": True, **result}
        return {"ok": False, "error": f"Tab index {index} not found."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def execute_browser_close(arguments: dict[str, Any]) -> dict[str, Any]:
    logger.info("browser_close called")

    async def _close():
        global _browser_instance, _playwright_browser
        if _browser_instance:
            try:
                await _browser_instance.close()
            except Exception:
                pass
            _browser_instance = None
            _playwright_browser = None

    try:
        loop = _ensure_loop()
        future = asyncio.run_coroutine_threadsafe(_close(), loop)
        future.result(timeout=15)
        return {"ok": True, "message": "Browser closed."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
