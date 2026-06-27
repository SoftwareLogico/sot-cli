"""
Browser tools for sot-cli — Powered natively by browser-use (Driver only, NO Agent).

sot-cli is the brain. browser-use is just the hands and eyes.
Uses the official unified BrowserSession API of browser-use 0.13.1, bypassing its custom wrappers.
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

# Importamos la clase unificada Browser de browser-use
from browser_use import Browser

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

# Estado global de la sesión única de browser-use
_browser: Browser | None = None

async def _get_context():
    """Safely retrieves the raw Playwright BrowserContext from the browser-use Browser instance."""
    global _browser
    if not _browser:
        return None
    
    pw_browser = None
    if hasattr(_browser, "get_playwright_browser"):
        pw_browser = await _browser.get_playwright_browser()
    elif hasattr(_browser, "browser") and hasattr(_browser.browser, "get_playwright_browser"):
        pw_browser = await _browser.browser.get_playwright_browser()
        
    if pw_browser and pw_browser.contexts:
        return pw_browser.contexts[0]
    return None

async def _get_page():
    """Safely retrieves the raw Playwright Page from the browser-use Browser instance.
    This bypasses browser-use's custom Page wrapper to prevent AttributeError and Page.goto issues.
    """
    global _browser
    if not _browser:
        return None
    
    try:
        # get_current_page() nos devuelve el Wrapper "Page" de browser-use
        bu_page = await _browser.get_current_page()
        if bu_page is not None:
            # Extraemos la página de Playwright real (guardada en el atributo .page)
            if hasattr(bu_page, "page"):
                return bu_page.page
            return bu_page
    except Exception:
        pass

    # Fallback asíncrono directo por si acaso
    context = await _get_context()
    if context:
        pages = context.pages
        return pages[0] if pages else await context.new_page()
    return None

def execute_browser_open(arguments: dict[str, Any]) -> dict[str, Any]:
    profile = arguments.get("profile", "fresh")
    url = arguments.get("url", "")
    logger.info(f"browser_open called: profile={profile!r} url={url!r}")

    async def _open():
        global _browser

        if _browser is not None:
            try:
                # Browser/BrowserSession tiene .stop() o .close() para finalizar la instancia
                if hasattr(_browser, "stop"):
                    await _browser.stop()
                elif hasattr(_browser, "close"):
                    await _browser.close()
            except Exception:
                pass
            _browser = None

        from browser_use import Browser

        if profile == "fresh":
            _browser = Browser()
        else:
            from sot_cli.tools.browser.profiles import list_browser_profiles
            profiles = list_browser_profiles()
            matched = next((p for p in profiles if p["browser"].lower() == profile.lower()), None)
            
            if not matched:
                raise RuntimeError(f"Profile '{profile}' not found. Available: {[p['browser'] for p in profiles]}")

            profile_dir = matched["profile_dir"]
            logger.info(f"Delegating to browser-use native from_system_chrome: {matched['browser']}/{profile_dir}")

            # Método nativo oficial de browser-use para clonar y levantar el perfil
            _browser = Browser.from_system_chrome(profile_directory=profile_dir)

        # Iniciamos el navegador (clonación y CDP interna de browser-use)
        await _browser.start()
        
        # Obtenemos la página nativa de Playwright de forma segura (Bypass del wrapper)
        page = await _get_page()
        if page is None:
            raise RuntimeError("Browser opened but could not initialize a raw Playwright page.")

        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

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
    if not url: return {"ok": False, "error": "url is required"}

    async def _nav():
        page = await _get_page()
        if not page: return None
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {"title": await page.title(), "url": page.url}

    try:
        result = _run_async(_nav())
        if result: return {"ok": True, **result}
        return {"ok": False, "error": "No browser open. Use browser_open first."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def execute_browser_screenshot(arguments: dict[str, Any]) -> dict[str, Any]:
    full_page = arguments.get("full_page", False)

    async def _screenshot():
        page = await _get_page()
        if not page: return None
        img_bytes = await page.screenshot(full_page=full_page)
        b64 = base64.b64encode(img_bytes).decode("ascii")
        return {"title": await page.title(), "url": page.url, "screenshot_base64": b64, "size_bytes": len(img_bytes)}

    try:
        result = _run_async(_screenshot())
        if result: return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def execute_browser_click(arguments: dict[str, Any]) -> dict[str, Any]:
    x = arguments.get("x")
    y = arguments.get("y")
    if x is None or y is None: return {"ok": False, "error": "x and y are required"}

    async def _click():
        page = await _get_page()
        if not page: return None
        await page.mouse.click(x, y)
        await asyncio.sleep(0.5)
        return {"title": await page.title(), "url": page.url, "clicked": {"x": x, "y": y}}

    try:
        result = _run_async(_click())
        if result: return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def execute_browser_type(arguments: dict[str, Any]) -> dict[str, Any]:
    text = arguments.get("text", "")
    press_enter = arguments.get("press_enter", False)
    if not text: return {"ok": False, "error": "text is required"}

    async def _type():
        page = await _get_page()
        if not page: return None
        await page.keyboard.type(text, delay=50)
        if press_enter:
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.5)
        return {"typed": text, "pressed_enter": press_enter}

    try:
        result = _run_async(_type())
        if result: return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def execute_browser_key(arguments: dict[str, Any]) -> dict[str, Any]:
    key = arguments.get("key", "")
    if not key: return {"ok": False, "error": "key is required"}

    async def _key():
        page = await _get_page()
        if not page: return None
        await page.keyboard.press(key)
        return {"pressed": key}

    try:
        result = _run_async(_key())
        if result: return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def execute_browser_scroll(arguments: dict[str, Any]) -> dict[str, Any]:
    direction = arguments.get("direction", "down")
    amount = arguments.get("amount", 500)
    delta = amount if direction == "down" else -amount

    async def _scroll():
        page = await _get_page()
        if not page: return None
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(0.3)
        return {"title": await page.title(), "url": page.url, "scrolled": {"direction": direction, "amount": amount}}

    try:
        result = _run_async(_scroll())
        if result: return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def execute_browser_get_html(arguments: dict[str, Any]) -> dict[str, Any]:
    max_length = arguments.get("max_length", 5000)

    async def _html():
        page = await _get_page()
        if not page: return None
        content = await page.content()
        truncated = content[:max_length] if len(content) > max_length else content
        return {"title": await page.title(), "url": page.url, "html": truncated, "total_length": len(content)}

    try:
        result = _run_async(_html())
        if result: return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def execute_browser_get_text(arguments: dict[str, Any]) -> dict[str, Any]:
    max_length = arguments.get("max_length", 5000)

    async def _text():
        page = await _get_page()
        if not page: return None
        content = await page.evaluate("() => document.body.innerText")
        truncated = content[:max_length] if len(content) > max_length else content
        return {"title": await page.title(), "url": page.url, "text": truncated, "total_length": len(content)}

    try:
        result = _run_async(_text())
        if result: return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def execute_browser_back(arguments: dict[str, Any]) -> dict[str, Any]:
    async def _back():
        page = await _get_page()
        if not page: return None
        await page.go_back(wait_until="domcontentloaded", timeout=15000)
        return {"title": await page.title(), "url": page.url}

    try:
        result = _run_async(_back())
        if result: return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def execute_browser_forward(arguments: dict[str, Any]) -> dict[str, Any]:
    async def _forward():
        page = await _get_page()
        if not page: return None
        await page.go_forward(wait_until="domcontentloaded", timeout=15000)
        return {"title": await page.title(), "url": page.url}

    try:
        result = _run_async(_forward())
        if result: return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def execute_browser_tab_new(arguments: dict[str, Any]) -> dict[str, Any]:
    url = arguments.get("url", "")
    async def _new_tab():
        global _browser
        if not _browser: return None
        bu_page = await _browser.new_page()
        page = bu_page.page if hasattr(bu_page, "page") else bu_page
        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {"title": await page.title(), "url": page.url}

    try:
        result = _run_async(_new_tab())
        if result: return {"ok": True, **result}
        return {"ok": False, "error": "No browser open."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def execute_browser_tab_list(arguments: dict[str, Any]) -> dict[str, Any]:
    async def _list_tabs():
        global _browser
        if not _browser: return []
        
        bu_pages = await _browser.get_pages()
        tabs = []
        for i, bu_page in enumerate(bu_pages):
            page = bu_page.page if hasattr(bu_page, "page") else bu_page
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
        global _browser
        if not _browser: return None
        
        bu_pages = await _browser.get_pages()
        if 0 <= index < len(bu_pages):
            bu_page = bu_pages[index]
            page = bu_page.page if hasattr(bu_page, "page") else bu_page
            await page.bring_to_front()
            return {"title": await page.title(), "url": page.url}
        return None

    try:
        result = _run_async(_switch())
        if result: return {"ok": True, **result}
        return {"ok": False, "error": f"Tab index {index} not found."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def execute_browser_close(arguments: dict[str, Any]) -> dict[str, Any]:
    logger.info("browser_close called")

    async def _close():
        global _browser
        if _browser:
            try: 
                if hasattr(_browser, "stop"):
                    await _browser.stop()
                elif hasattr(_browser, "close"):
                    await _browser.close()
            except Exception: pass
            _browser = None

    try:
        loop = _ensure_loop()
        future = asyncio.run_coroutine_threadsafe(_close(), loop)
        future.result(timeout=15)
        return {"ok": True, "message": "Browser closed."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}