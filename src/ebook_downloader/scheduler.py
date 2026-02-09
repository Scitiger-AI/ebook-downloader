"""并发调度与重试"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from .browser import BrowserManager, CDNResult
from .config import Config
from .downloader import download_file, extract_ebook
from .models import Book, DownloadRecord, DownloadStatus
from .state import StateDB
from .utils import DownloadProgressManager

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    """清理文件名，移除非法字符"""
    # 替换 Windows/Unix 非法字符
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    # 截断过长文件名（保留扩展名）
    if len(name.encode("utf-8")) > 200:
        name = name[:60]
    return name.strip(". ")


class Scheduler:
    """下载调度器：管理并发下载任务"""

    def __init__(
        self,
        config: Config,
        state: StateDB,
        browser: BrowserManager,
        progress: DownloadProgressManager,
    ) -> None:
        self.config = config
        self.state = state
        self.browser = browser
        self.progress = progress

    async def run(self, books: list[Book]) -> dict[str, int]:
        """调度下载任务

        Returns:
            统计结果 {status: count}
        """
        # 过滤已完成的书籍
        completed_uids = await self.state.get_completed_uids()
        pending = [b for b in books if b.uid not in completed_uids]

        if not pending:
            logger.info("所有书籍已下载完成，无需操作")
            return {"skipped": len(books)}

        logger.info(
            "待下载: %d / %d (已完成 %d)",
            len(pending), len(books), len(completed_uids),
        )

        # 初始化总体进度
        self.progress.set_total(len(pending))

        # 使用 Semaphore 控制整体并发（浏览器+下载共用）
        semaphore = asyncio.Semaphore(self.config.browser_concurrency)
        stats = {"completed": 0, "failed": 0, "skipped": len(books) - len(pending)}

        tasks = [
            self._worker(book, semaphore, stats)
            for book in pending
        ]
        await asyncio.gather(*tasks)

        return stats

    async def _worker(
        self,
        book: Book,
        semaphore: asyncio.Semaphore,
        stats: dict[str, int],
    ) -> None:
        """单本书下载 worker"""
        record = DownloadRecord(
            book_uid=book.uid,
            title=book.title,
            author=book.author,
            category=book.category,
            link=book.link,
        )

        for attempt in range(1, self.config.max_retries + 1):
            try:
                # 获取 CDN 链接（受浏览器并发限制）
                record.status = DownloadStatus.DOWNLOADING
                record.retry_count = attempt - 1
                await self.state.upsert(record)

                cdn_result = await self.browser.fetch_cdn_url(book)
                record.cdn_url = cdn_result.url

                # 确定目标路径
                filename = cdn_result.filename or sanitize_filename(book.title)
                if not Path(filename).suffix:
                    # 默认使用 epub
                    filename += ".epub"

                category_dir = sanitize_filename(book.category) or "未分类"
                dest = self.config.download_path / category_dir / filename

                # 创建下载进度任务
                task_id = self.progress.add_task(book.title)

                def on_progress(downloaded: int, total: int, chunk: int) -> None:
                    self.progress.update_task(task_id, downloaded, total)

                # HTTP 下载（不再受 Semaphore 限制）
                result_path = await download_file(
                    cdn_result.url, dest, self.config, progress_cb=on_progress,
                )

                # 解压 ZIP → 电子书文件
                clean_title = sanitize_filename(book.title)
                ebook_files = extract_ebook(
                    result_path,
                    book_title=clean_title,
                    formats=self.config.extract_formats,
                    keep_zip=self.config.keep_zip,
                )

                # 更新记录（file_path 记录解压后的首个电子书路径）
                record.status = DownloadStatus.COMPLETED
                if ebook_files:
                    record.file_path = str(ebook_files[0])
                    record.file_size = sum(f.stat().st_size for f in ebook_files)
                else:
                    record.file_path = str(result_path)
                    record.file_size = result_path.stat().st_size if result_path.exists() else 0
                record.error_msg = ""
                await self.state.upsert(record)

                self.progress.complete_task(task_id)
                self.progress.advance_overall()
                stats["completed"] += 1
                return

            except Exception as e:
                logger.warning(
                    "下载失败 (%s) 尝试 %d/%d: %s",
                    book.title, attempt, self.config.max_retries, e,
                )
                record.error_msg = str(e)

                if attempt < self.config.max_retries:
                    backoff = self.config.retry_backoff * (2 ** (attempt - 1))
                    logger.debug("等待 %d 秒后重试...", backoff)
                    await asyncio.sleep(backoff)

        # 所有重试均失败
        record.status = DownloadStatus.FAILED
        record.retry_count = self.config.max_retries
        await self.state.upsert(record)
        stats["failed"] += 1
        logger.error("下载最终失败: %s — %s", book.title, record.error_msg)
