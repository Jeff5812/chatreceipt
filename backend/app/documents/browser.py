"""Print document HTML to PDF and PNG with a headless Chromium-based browser (Playwright).

Playwright runs on its own event loop in a background thread. That keeps it independent of the
server's event loop (uvicorn's reload mode on Windows uses one that can't start subprocesses)
and lets one browser instance serve every request.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import Any, Literal

DocumentFormat = Literal["pdf", "png"]
PAGE_WIDTH_PX = 794  # A4 width at 96 dpi
PAGE_HEIGHT_PX = 1123
# The image is the receipt on its desk, sized for phones and chat apps: 540 CSS px wide, 1080 px at 2x.
IMAGE_WIDTH_PX = 540


class DocumentRenderError(Exception):
    def __init__(self, kind: Literal["unavailable", "timeout", "failed"], detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


async def _block_request(route: Any) -> None:
    await route.abort()


class BrowserRenderer:
    def __init__(self, *, channel: str = "", timeout_seconds: float = 30.0) -> None:
        self._channel = channel or None
        self._timeout = timeout_seconds
        self._thread_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._launch_lock: asyncio.Lock | None = None
        self._playwright: Any = None
        self._browser: Any = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._thread_lock:
            if self._loop is None:
                if sys.platform == "win32":
                    loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()
                else:
                    loop = asyncio.new_event_loop()
                thread = threading.Thread(target=loop.run_forever, name="document-renderer", daemon=True)
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop

    async def _get_browser(self) -> Any:
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()
        async with self._launch_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise DocumentRenderError("unavailable", "Playwright isn't installed") from exc
            try:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(channel=self._channel, headless=True)
            except Exception as exc:
                raise DocumentRenderError("unavailable", f"couldn't start the browser ({type(exc).__name__})") from exc
            return self._browser

    async def _render(self, html: str, fmt: DocumentFormat) -> bytes:
        browser = await self._get_browser()
        context = await browser.new_context(
            viewport={"width": PAGE_WIDTH_PX if fmt == "pdf" else IMAGE_WIDTH_PX, "height": PAGE_HEIGHT_PX},
            device_scale_factor=2 if fmt == "png" else 1,
        )
        try:
            # The document is self-contained; refuse every network request it might make.
            await context.route("**/*", _block_request)
            page = await context.new_page()
            await page.set_content(html, wait_until="load")
            await page.evaluate("document.fonts.ready.then(() => true)")
            if fmt == "pdf":
                return await page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
            await page.emulate_media(media="screen")
            return await page.screenshot(full_page=True, type="png")
        finally:
            await context.close()

    async def render(self, html: str, fmt: DocumentFormat) -> bytes:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(self._render(html, fmt), loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=self._timeout)
        except TimeoutError as exc:
            future.cancel()
            raise DocumentRenderError("timeout", f"took longer than {self._timeout:g}s") from exc
        except DocumentRenderError:
            raise
        except Exception as exc:
            raise DocumentRenderError("failed", type(exc).__name__) from exc

    def close(self) -> None:
        if self._loop is None:
            return

        async def shutdown() -> None:
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
