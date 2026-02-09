"""工具模块：日志配置、进度显示、文件名清理"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

console = Console()


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    """配置日志：同时输出到控制台和文件"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "ebook-downloader.log"

    level = logging.DEBUG if verbose else logging.INFO

    # Rich 控制台处理器
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        rich_tracebacks=True,
    )
    rich_handler.setLevel(level)

    # 文件处理器
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )

    # 根日志器
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(rich_handler)
    root.addHandler(file_handler)

    # 抑制第三方库的 DEBUG 日志
    for name in ("httpx", "httpcore", "playwright", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


class DownloadProgressManager:
    """基于 Rich 的多任务下载进度显示"""

    def __init__(self) -> None:
        # 总体进度
        self._overall = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]总体进度"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        )
        # 单文件下载进度
        self._files = Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console,
        )
        self._overall_task_id = None

    def set_total(self, total: int) -> None:
        """设置总任务数"""
        self._overall_task_id = self._overall.add_task("下载", total=total)

    def add_task(self, description: str) -> int:
        """添加单文件下载任务"""
        # 截断过长的描述
        if len(description) > 40:
            description = description[:37] + "..."
        return self._files.add_task(description, total=None)

    def update_task(self, task_id: int, downloaded: int, total: int) -> None:
        """更新单文件进度"""
        self._files.update(task_id, completed=downloaded, total=total or None)

    def complete_task(self, task_id: int) -> None:
        """标记单文件下载完成"""
        self._files.remove_task(task_id)

    def advance_overall(self, advance: int = 1) -> None:
        """推进总体进度"""
        if self._overall_task_id is not None:
            self._overall.advance(self._overall_task_id, advance)

    def __enter__(self) -> DownloadProgressManager:
        self._overall.start()
        self._files.start()
        return self

    def __exit__(self, *args) -> None:
        self._files.stop()
        self._overall.stop()


def print_stats_table(stats: dict[str, int], total_size: int = 0) -> None:
    """打印统计表格"""
    table = Table(title="下载统计", show_header=True, header_style="bold magenta")
    table.add_column("状态", style="cyan")
    table.add_column("数量", justify="right", style="green")

    status_labels = {
        "pending": "⏳ 等待中",
        "downloading": "⬇️  下载中",
        "completed": "✅ 已完成",
        "failed": "❌ 失败",
        "skipped": "⏭️  已跳过",
    }

    total = 0
    for status, label in status_labels.items():
        count = stats.get(status, 0)
        total += count
        if count > 0:
            table.add_row(label, str(count))

    table.add_row("─" * 10, "─" * 6, style="dim")
    table.add_row("📚 总计", str(total), style="bold")

    if total_size > 0:
        table.add_row("💾 已下载大小", _format_size(total_size), style="bold")

    console.print(table)


def _format_size(size: int) -> str:
    """格式化文件大小"""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
