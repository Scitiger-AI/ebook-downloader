"""浏览器管理与 CDN 链接提取

核心流程（基于实际抓包验证）：
1. Playwright 打开城通网盘页面（触发 getfile.php 获取文件信息）
2. 注册 response 监听
3. 点击"普通下载·立即下载"按钮（触发 get_file_url.php → get_down_url.php）
4. 从 get_file_url.php 或 get_down_url.php 响应 JSON 的 downurl 字段提取 CDN 直链

实际 API 调用链：
  getfile.php → (点击下载) → get_file_url.php → get_down_url.php → CDN 下载
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import unquote

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)

from .config import Config
from .models import Book

logger = logging.getLogger(__name__)


@dataclass
class CDNResult:
    """CDN 链接提取结果"""
    url: str
    filename: str = ""
    file_size: int = 0


class BrowserManager:
    """浏览器生命周期管理 + CDN 链接提取"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._semaphore = asyncio.Semaphore(config.browser_concurrency)

    async def start(self) -> None:
        """启动 Playwright 和浏览器实例"""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
        )
        logger.info(
            "浏览器已启动 (headless=%s, concurrency=%d)",
            self.config.headless, self.config.browser_concurrency,
        )

    async def stop(self) -> None:
        """关闭浏览器和 Playwright"""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("浏览器已关闭")

    async def fetch_cdn_url(self, book: Book) -> CDNResult:
        """获取书籍的 CDN 下载链接

        使用 Semaphore 控制并发 Context 数量。
        获取链接后立即释放 Context，不阻塞后续任务。
        """
        async with self._semaphore:
            return await self._extract_cdn_url(book)

    async def _extract_cdn_url(self, book: Book) -> CDNResult:
        """在独立 Context 中提取 CDN 链接"""
        if not self._browser:
            raise RuntimeError("浏览器未启动")

        context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        try:
            page = await context.new_page()
            result = await self._navigate_and_extract(page, book)
            return result
        finally:
            await context.close()

    async def _navigate_and_extract(self, page: Page, book: Book) -> CDNResult:
        """页面操作：打开链接 → 拦截 API → 点击下载 → 提取 CDN"""
        timeout_ms = self.config.browser_timeout * 1000

        # 存储拦截到的 CDN URL
        cdn_future: asyncio.Future[CDNResult] = asyncio.get_running_loop().create_future()

        async def on_response(response: Response) -> None:
            """监听 API 响应，提取 CDN 链接"""
            url = response.url
            try:
                # 拦截 get_file_url.php 或 get_down_url.php
                if ("get_file_url" in url or "get_down_url" in url) and response.status == 200:
                    body = await response.text()
                    result = _parse_cdn_response(body)
                    if result and not cdn_future.done():
                        logger.debug("从 %s 获取到 CDN 链接", url.split("?")[0].split("/")[-1])
                        cdn_future.set_result(result)
            except Exception as e:
                logger.debug("处理响应失败 (%s): %s", url[:80], e)

        page.on("response", on_response)

        try:
            # 导航到下载页面
            logger.debug("正在打开: %s (%s)", book.title, book.link)
            await page.goto(book.link, wait_until="domcontentloaded", timeout=timeout_ms)

            # 等待页面完全加载（城通网盘是 SPA，需要等 JS 渲染）
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)

            # 点击"普通下载·立即下载"按钮
            await self._click_download_button(page)

            # 等待 CDN URL 被拦截
            result = await asyncio.wait_for(cdn_future, timeout=self.config.browser_timeout)
            logger.info("获取CDN链接: %s → %s", book.title, result.url[:100])
            return result

        except asyncio.TimeoutError:
            raise TimeoutError(f"获取 CDN 链接超时: {book.title}")
        except Exception as e:
            raise RuntimeError(f"获取 CDN 链接失败 ({book.title}): {e}") from e

    async def _click_download_button(self, page: Page) -> None:
        """点击"普通下载·立即下载"按钮

        基于实际页面结构，按优先级尝试多种选择器。
        城通网盘页面结构：#freeDownloadNormal 区域内的 button
        """
        # 按优先级排列的选择器策略
        strategies = [
            # 策略1: 城通网盘特有的普通下载区域按钮（最精确）
            ('#freeDownloadNormal button:has-text("立即下载")', "freeDownloadNormal button"),
            # 策略2: 第一个"立即下载"按钮
            ('button:has-text("立即下载")', "first button with 立即下载"),
            # 策略3: 包含"立即下载"的链接
            ('a:has-text("立即下载")', "first link with 立即下载"),
        ]

        for selector, desc in strategies:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=3000):
                    await el.click(timeout=5000)
                    logger.debug("点击按钮成功: %s", desc)
                    return
            except Exception:
                continue

        # 最后兜底：点击任何包含"下载"文字的按钮
        try:
            el = page.get_by_role("button", name="立即下载").first
            await el.click(timeout=5000)
            logger.debug("通过 role 匹配点击按钮")
            return
        except Exception:
            pass

        logger.warning("未找到下载按钮")
        raise RuntimeError("未找到下载按钮")


def _parse_cdn_response(body: str) -> CDNResult | None:
    """解析 get_file_url.php / get_down_url.php 返回的 JSON

    已验证的响应格式：
    {
        "code": 200,
        "downurl": "https://88-cucc-data.tv002.com/d.../file.zip?...",
        "file_size": 2604814,
        "file_name": "10019-智慧未来.zip",
        "xhr": true
    }
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    # code 检查
    if data.get("code") != 200:
        logger.debug("API 返回非 200 code: %s", data.get("code"))
        return None

    # 提取 downurl
    cdn_url = data.get("downurl") or data.get("down_url") or data.get("url")

    if not cdn_url or not isinstance(cdn_url, str):
        return None

    # 提取文件名：优先用 API 返回的 file_name
    filename = data.get("file_name", "")
    if not filename:
        # 从 URL 的 fname 参数提取
        fname_match = re.search(r'fname=([^&]+)', cdn_url)
        if fname_match:
            filename = unquote(fname_match.group(1))
        else:
            # 从 URL 路径提取
            filename = cdn_url.split("/")[-1].split("?")[0]

    file_size = data.get("file_size", 0)

    return CDNResult(url=cdn_url, filename=filename, file_size=file_size)
