"""生产者-消费者并发调度器

架构：
  生产者 (CDN Fetcher)      asyncio.Queue       消费者 (Downloader)
  browser_semaphore + delay ──put──> CDNTask ──get──> 纯HTTP下载+ZIP解压
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from .browser import BrowserManager, CDNResult, _is_proxy_error
from .config import Config
from .downloader import FileTooLargeError, download_file, extract_ebook
from .models import Book, DownloadRecord, DownloadStatus
from .state import StateDB
from .utils import DownloadProgressManager

logger = logging.getLogger(__name__)


def _is_permanent_error(exc: Exception) -> bool:
    """判断是否为不可恢复的错误（重试无意义）"""
    if isinstance(exc, (zlib.error, zipfile.BadZipFile, FileTooLargeError)):
        return True
    msg = str(exc)
    # ZIP 文件损坏
    if "Bad CRC-32" in msg or "decompressing data" in msg:
        return True
    # 无效链接（数据源中 link 字段为非法值，如 "链接未找到"）
    if "Cannot navigate to invalid URL" in msg:
        return True
    return False


def _is_cdn_expired(exc: Exception) -> bool:
    """判断是否为 CDN 链接过期错误（403/404/410）"""
    msg = str(exc).lower()
    for code in ("403", "404", "410"):
        if code in msg:
            return True
    return False


def sanitize_filename(name: str) -> str:
    """清理文件名，移除非法字符"""
    # 替换 Windows/Unix 非法字符
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    # 截断过长文件名（保留扩展名）
    if len(name.encode("utf-8")) > 200:
        name = name[:60]
    return name.strip(". ")


@dataclass
class CDNTask:
    """Queue 中传递的消息：已获取 CDN 链接的下载任务"""
    book: Book
    record: DownloadRecord
    cdn_result: CDNResult
    dest: Path


class Scheduler:
    """下载调度器：生产者-消费者架构

    生产者：浏览器获取 CDN 链接（受 browser._semaphore + smart_delay 限流）
    消费者：纯 HTTP 下载 + ZIP 解压（受 download_concurrency 控制并发数）
    """

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
        # 用于智能延迟：记录上次浏览器访问时间
        self._last_browser_access: float = 0.0
        self._access_lock = asyncio.Lock()  # 保护 _last_browser_access

    async def run(self, books: list[Book]) -> dict[str, int]:
        """调度下载任务

        Returns:
            统计结果 {status: count}
        """
        # 启动前清理：残留 .part 文件 + 卡在 downloading 状态的记录
        await self._cleanup_stale()

        # 过滤已完成和已跳过（文件损坏）的书籍
        skip_uids = await self.state.get_skip_uids()
        pending = [b for b in books if b.uid not in skip_uids]

        if not pending:
            logger.info("所有书籍已下载完成或已跳过，无需操作")
            return {"skipped": len(books)}

        # 统计已完成和已跳过的分别数量
        completed_count = len(await self.state.get_completed_uids())
        skipped_count = len(skip_uids) - completed_count

        logger.info(
            "待下载: %d / %d (已完成 %d, 已跳过 %d)",
            len(pending), len(books), completed_count, skipped_count,
        )

        # 初始化总体进度
        self.progress.set_total(len(pending))

        stats = {"completed": 0, "failed": 0, "skipped": len(books) - len(pending)}

        logger.info(
            "生产者-消费者模式: 浏览器并发=%d, 下载并发=%d, 队列容量=%d",
            self.config.browser_concurrency,
            self.config.download_concurrency,
            self.config.cdn_queue_size,
        )

        # 创建任务队列
        queue: asyncio.Queue[CDNTask | None] = asyncio.Queue(
            maxsize=self.config.cdn_queue_size,
        )

        # 启动生产者（1个协程，内部并发受 browser._semaphore + smart_delay 控制）
        producer = asyncio.create_task(
            self._cdn_producer(pending, queue, stats),
            name="cdn-producer",
        )

        # 启动 N 个消费者
        num_consumers = self.config.download_concurrency
        consumers = [
            asyncio.create_task(
                self._download_consumer(i, queue, stats),
                name=f"download-consumer-{i}",
            )
            for i in range(num_consumers)
        ]

        # 等待生产者完成
        await producer

        # 生产者完成后发送 N 个 sentinel 通知消费者退出
        for _ in range(num_consumers):
            await queue.put(None)

        # 等待所有消费者处理完毕
        await asyncio.gather(*consumers)

        return stats

    async def _cdn_producer(
        self,
        books: list[Book],
        queue: asyncio.Queue[CDNTask | None],
        stats: dict[str, int],
    ) -> None:
        """生产者：并发获取 CDN 链接，成功后放入队列

        内部并发控制：
        - cdn_semaphore 限制排队等待的协程数量，避免过多协程堆积
        - browser._semaphore 控制实际浏览器并发（在 browser.fetch_cdn_url 内部）
        - smart_delay 控制访问间隔（反爬策略不变）
        """
        cdn_semaphore = asyncio.Semaphore(self.config.browser_concurrency * 2)

        async def fetch_one(book: Book) -> None:
            async with cdn_semaphore:
                await self._fetch_cdn_and_enqueue(book, queue, stats)

        tasks = [fetch_one(book) for book in books]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_cdn_and_enqueue(
        self,
        book: Book,
        queue: asyncio.Queue[CDNTask | None],
        stats: dict[str, int],
    ) -> None:
        """获取单本书的 CDN 链接，成功则入队，失败则标记状态"""
        record = DownloadRecord(
            book_uid=book.uid,
            title=book.title,
            author=book.author,
            category=book.category,
            link=book.link,
        )

        last_error: Exception | None = None

        for attempt in range(1, self.config.max_retries + 1):
            try:
                # 智能延迟：模拟真人行为
                await self._smart_delay()

                # 更新状态为 DOWNLOADING
                record.status = DownloadStatus.DOWNLOADING
                record.retry_count = attempt - 1
                await self.state.upsert(record)

                # 获取 CDN 链接（受 browser._semaphore 限制）
                cdn_result = await self.browser.fetch_cdn_url(book)
                record.cdn_url = cdn_result.url

                # 计算目标路径
                filename = cdn_result.filename or sanitize_filename(book.title)
                if not Path(filename).suffix:
                    filename += ".epub"

                category_dir = sanitize_filename(book.category) or "未分类"
                dest = self.config.download_path / category_dir / filename

                # 构造 CDNTask 入队
                task = CDNTask(
                    book=book,
                    record=record,
                    cdn_result=cdn_result,
                    dest=dest,
                )
                await queue.put(task)
                logger.debug("CDN 链接已入队: %s → %s", book.title, cdn_result.url[:80])
                return

            except Exception as e:
                last_error = e
                logger.warning(
                    "CDN获取失败 (%s) 尝试 %d/%d: %s",
                    book.title, attempt, self.config.max_retries, e,
                )
                record.error_msg = str(e)

                # 永久错误无需重试
                if _is_permanent_error(e):
                    break

                # 代理切换
                if self.browser.proxy_pool:
                    err_msg = str(e)
                    if "未找到下载按钮" in err_msg or _is_proxy_error(e):
                        await self.browser.proxy_pool.invalidate()
                        logger.info("代理异常，已触发切换: %s", book.title)

                if attempt < self.config.max_retries:
                    backoff = self.config.retry_backoff * (2 ** (attempt - 1))
                    logger.debug("等待 %d 秒后重试...", backoff)
                    await asyncio.sleep(backoff)

        # 所有重试均失败
        record.retry_count = self.config.max_retries
        if last_error is not None and _is_permanent_error(last_error):
            record.status = DownloadStatus.SKIPPED
            await self.state.upsert(record)
            stats["failed"] += 1
            logger.warning("不可恢复错误，已标记跳过: %s — %s", book.title, record.error_msg)
        else:
            record.status = DownloadStatus.FAILED
            await self.state.upsert(record)
            stats["failed"] += 1
            logger.error("CDN获取最终失败: %s — %s", book.title, record.error_msg)

        # 推进总体进度（失败也算处理完成）
        self.progress.advance_overall()

    async def _download_consumer(
        self,
        consumer_id: int,
        queue: asyncio.Queue[CDNTask | None],
        stats: dict[str, int],
    ) -> None:
        """消费者：从队列获取任务，执行 HTTP 下载 + ZIP 解压"""
        while True:
            task = await queue.get()
            if task is None:
                queue.task_done()
                break

            try:
                await self._execute_download(task, stats)
            except Exception as e:
                logger.error(
                    "消费者 %d 异常: %s — %s", consumer_id, task.book.title, e,
                )
            finally:
                queue.task_done()

    async def _execute_download(
        self,
        task: CDNTask,
        stats: dict[str, int],
    ) -> None:
        """执行单个下载任务：HTTP 下载 → ZIP 解压 → 更新状态

        独立重试逻辑（max_download_retries）：
        - 403/404/410 → CDN 链接过期，不重试
        - 网络超时/断开 → 短暂退避后重试
        - BadZipFile/FileTooLarge → 永久错误，标记 SKIPPED
        """
        book = task.book
        record = task.record
        task_id: int | None = None

        for attempt in range(1, self.config.max_download_retries + 1):
            try:
                # 创建进度条任务
                task_id = self.progress.add_task(book.title)

                def on_progress(downloaded: int, total: int, chunk: int) -> None:
                    self.progress.update_task(task_id, downloaded, total)

                # HTTP 下载
                result_path = await download_file(
                    task.cdn_result.url, task.dest, self.config,
                    progress_cb=on_progress,
                )

                # 解压 ZIP → 电子书文件
                clean_title = sanitize_filename(book.title)
                ebook_files = extract_ebook(
                    result_path,
                    book_title=clean_title,
                    formats=self.config.extract_formats,
                    keep_zip=self.config.keep_zip,
                )

                # 更新记录
                record.status = DownloadStatus.COMPLETED
                if ebook_files:
                    record.file_path = str(ebook_files[0])
                    record.file_size = sum(f.stat().st_size for f in ebook_files)
                else:
                    record.file_path = str(result_path)
                    record.file_size = (
                        result_path.stat().st_size if result_path.exists() else 0
                    )
                record.error_msg = ""
                await self.state.upsert(record)

                self.progress.complete_task(task_id)
                self.progress.advance_overall()
                stats["completed"] += 1
                return

            except Exception as e:
                logger.warning(
                    "下载失败 (%s) 尝试 %d/%d: %s",
                    book.title, attempt, self.config.max_download_retries, e,
                )
                record.error_msg = str(e)

                # 清理当前进度条任务
                if task_id is not None:
                    self.progress.complete_task(task_id)
                    task_id = None

                # CDN 链接过期 → 不重试（用户可用 retry 命令重新获取）
                if _is_cdn_expired(e):
                    logger.warning("CDN链接已过期，标记失败: %s", book.title)
                    break

                # 永久错误 → 标记 SKIPPED
                if _is_permanent_error(e):
                    record.status = DownloadStatus.SKIPPED
                    await self.state.upsert(record)
                    stats["failed"] += 1
                    self.progress.advance_overall()
                    logger.warning(
                        "不可恢复错误，已标记跳过: %s — %s",
                        book.title, record.error_msg,
                    )
                    return

                # 临时错误 → 短暂退避后重试
                if attempt < self.config.max_download_retries:
                    backoff = 3 * attempt  # 3s, 6s
                    logger.debug("下载重试等待 %d 秒...", backoff)
                    await asyncio.sleep(backoff)

        # 所有下载重试均失败
        record.status = DownloadStatus.FAILED
        await self.state.upsert(record)
        stats["failed"] += 1
        self.progress.advance_overall()
        logger.error("下载最终失败: %s — %s", book.title, record.error_msg)

    # ────────────── 辅助方法（保留不变） ──────────────

    async def _cleanup_stale(self) -> None:
        """清理上次中断残留的 .part 文件和异常状态记录

        场景：Ctrl+C 中断后，downloads 目录下会残留未完成的 .part 文件，
        DB 中对应记录卡在 downloading 状态。
        - .part 文件无法续传（CDN 链接已过期），直接删除释放磁盘空间
        - downloading 状态重置为 pending，使其能被重新调度
        """
        # 1. 清理 .part 文件
        part_files = list(self.config.download_path.rglob("*.part"))
        if part_files:
            total_size = sum(f.stat().st_size for f in part_files if f.exists())
            for f in part_files:
                f.unlink(missing_ok=True)
            logger.info(
                "已清理 %d 个残留 .part 文件 (释放 %.1f MB)",
                len(part_files), total_size / (1024 * 1024),
            )

        # 2. 将卡在 downloading 状态的记录重置为 pending
        reset_count = await self.state.reset_downloading()
        if reset_count:
            logger.info("已重置 %d 条中断的 downloading 记录为 pending", reset_count)

    async def _smart_delay(self) -> None:
        """智能延迟：模拟真人行为，避免反爬

        在两次浏览器访问之间添加随机延迟，模拟真实用户的操作节奏。
        延迟时间：在 [request_min_delay, request_max_delay] 范围内随机选择。
        """
        if not self.config.enable_smart_delay:
            return

        async with self._access_lock:
            now = time.monotonic()
            elapsed = now - self._last_browser_access

            target_delay = random.uniform(
                self.config.request_min_delay,
                self.config.request_max_delay,
            )

            if self._last_browser_access > 0 and elapsed < target_delay:
                wait_time = target_delay - elapsed
                logger.debug(
                    "智能延迟: 等待 %.1f 秒 (目标间隔=%.1f秒, 已过=%.1f秒)",
                    wait_time, target_delay, elapsed,
                )
                await asyncio.sleep(wait_time)

            self._last_browser_access = time.monotonic()
