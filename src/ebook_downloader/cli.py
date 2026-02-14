"""CLI 命令定义"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .catalog import Catalog, fetch_catalog
from .config import Config, load_config
from .browser import BrowserManager
from .models import DownloadStatus
from .proxy import ProxyPool
from .scheduler import Scheduler
from .state import StateDB
from .utils import (
    DownloadProgressManager,
    console,
    print_stats_table,
    setup_logging,
)

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ebook-downloader",
        description="城通网盘电子书批量下载工具",
    )
    parser.add_argument(
        "--config", "-C", type=str, default=None,
        help="配置文件路径 (默认: config.yaml)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="启用详细日志",
    )

    sub = parser.add_subparsers(dest="command", help="可用命令")

    # download
    dl = sub.add_parser("download", help="下载电子书")
    dl.add_argument(
        "-c", "--categories", nargs="+", default=None,
        help="按分类筛选（可指定多个）",
    )
    dl.add_argument(
        "-k", "--keyword", type=str, default=None,
        help="按关键词搜索（标题/作者）",
    )
    dl.add_argument(
        "-n", "--limit", type=int, default=None,
        help="限制下载数量",
    )
    dl.add_argument(
        "--concurrent", type=int, default=None,
        help="覆盖浏览器并发数",
    )
    dl.add_argument(
        "--download-concurrent", type=int, default=None,
        help="覆盖 HTTP 下载并发数（独立于浏览器并发）",
    )
    dl.add_argument(
        "--no-headless", action="store_true",
        help="显示浏览器窗口（调试用）",
    )
    dl.add_argument(
        "--formats", type=str, default=None,
        help="提取的电子书格式，逗号分隔 (默认: epub)，如 epub,azw3,mobi",
    )
    dl.add_argument(
        "--keep-zip", action="store_true",
        help="解压后保留 ZIP 文件",
    )
    dl.add_argument(
        "-o", "--output-dir", type=str, default=None,
        help="指定下载目录（覆盖配置文件中的 download_dir）",
    )
    dl.add_argument(
        "--proxy-api", type=str, default=None,
        help="代理池 API 地址（如 https://dps.kdlapi.com/api/getdps/...）",
    )
    dl.add_argument(
        "--proxy-file", type=str, default=None,
        help="本地代理文件路径（每行一个 ip:port，与 --proxy-api 互斥）",
    )

    # list
    ls = sub.add_parser("list", help="列出书籍/分类")
    ls.add_argument(
        "--categories", action="store_true",
        help="列出所有分类",
    )
    ls.add_argument(
        "-c", "--category", nargs="+", default=None,
        help="列出指定分类的书籍",
    )
    ls.add_argument(
        "-k", "--keyword", type=str, default=None,
        help="按关键词搜索",
    )
    ls.add_argument(
        "-n", "--limit", type=int, default=20,
        help="显示条数 (默认: 20)",
    )

    # status
    sub.add_parser("status", help="查看下载统计")

    # retry
    rt = sub.add_parser("retry", help="重试所有失败项")
    rt.add_argument(
        "--proxy-api", type=str, default=None,
        help="代理池 API 地址（如 https://dps.kdlapi.com/api/getdps/...）",
    )
    rt.add_argument(
        "--proxy-file", type=str, default=None,
        help="本地代理文件路径（每行一个 ip:port，与 --proxy-api 互斥）",
    )
    rt.add_argument(
        "--no-headless", action="store_true",
        help="显示浏览器窗口（调试用）",
    )

    # fetch-data
    sub.add_parser("fetch-data", help="下载/更新 all-books.json 数据源")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    config = load_config(args.config)
    config.ensure_dirs()
    setup_logging(config.log_path, verbose=args.verbose)

    try:
        asyncio.run(_dispatch(args, config))
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/yellow]")
        sys.exit(130)
    except Exception as e:
        logger.exception("致命错误")
        console.print(f"[red]错误: {e}[/red]")
        sys.exit(1)


async def _dispatch(args: argparse.Namespace, config: Config) -> None:
    """命令分发"""
    match args.command:
        case "download":
            await _cmd_download(args, config)
        case "list":
            await _cmd_list(args, config)
        case "status":
            await _cmd_status(config)
        case "retry":
            await _cmd_retry(args, config)
        case "fetch-data":
            await _cmd_fetch_data(config)
        case _:
            console.print(f"[red]未知命令: {args.command}[/red]")


async def _cmd_download(args: argparse.Namespace, config: Config) -> None:
    """执行下载"""
    # 覆盖配置
    if args.output_dir:
        config.download_dir = args.output_dir
        config.download_path.mkdir(parents=True, exist_ok=True)
    if args.concurrent:
        config.browser_concurrency = args.concurrent
    if args.download_concurrent:
        config.download_concurrency = args.download_concurrent
    if args.no_headless:
        config.headless = False
    if args.formats:
        config.extract_formats = [f.strip() for f in args.formats.split(",")]
    if args.keep_zip:
        config.keep_zip = True

    catalog = Catalog(config)
    if not catalog.books:
        console.print("[red]无书籍数据，请先运行 fetch-data[/red]")
        return

    books = catalog.filter(
        categories=args.categories,
        exclude_categories=config.exclude_categories or None,
        keyword=args.keyword,
        limit=args.limit,
    )

    if not books:
        console.print("[yellow]未找到匹配的书籍[/yellow]")
        return

    console.print(f"[bold]筛选到 {len(books)} 本书籍，准备下载...[/bold]")

    state = StateDB(config.db_path)
    await state.open()

    # 创建代理池（--proxy-file 优先，其次 --proxy-api，最后配置文件）
    proxy_file = getattr(args, "proxy_file", None)
    proxy_api = getattr(args, "proxy_api", None) or config.proxy_api_url
    proxy_pool: ProxyPool | None = None

    if proxy_file:
        proxy_pool = ProxyPool(proxy_file=proxy_file)
        console.print(f"[bold blue]代理文件已启用: {proxy_file}[/bold blue]")
    elif proxy_api:
        proxy_pool = ProxyPool(api_url=proxy_api)
        console.print(f"[bold blue]代理池已启用: {proxy_api}[/bold blue]")

    browser = BrowserManager(config, proxy_pool=proxy_pool)
    await browser.start()

    try:
        progress = DownloadProgressManager()
        scheduler = Scheduler(config, state, browser, progress)

        with progress:
            stats = await scheduler.run(books)

        console.print()
        print_stats_table(stats, await state.total_size())

    finally:
        await browser.stop()
        await state.close()


async def _cmd_list(args: argparse.Namespace, config: Config) -> None:
    """列出书籍或分类"""
    catalog = Catalog(config)
    if not catalog.books:
        console.print("[red]无书籍数据，请先运行 fetch-data[/red]")
        return

    if args.categories:
        # 列出所有分类
        from rich.table import Table
        cats = catalog.categories()
        table = Table(title=f"书籍分类 (共 {len(cats)} 个)", show_header=True)
        table.add_column("分类", style="cyan")
        table.add_column("数量", justify="right", style="green")
        for cat, count in cats.items():
            table.add_row(cat, str(count))
        table.add_row("─" * 10, "─" * 6, style="dim")
        table.add_row("总计", str(sum(cats.values())), style="bold")
        console.print(table)
    else:
        # 列出书籍
        books = catalog.filter(
            categories=args.category,
            exclude_categories=config.exclude_categories or None,
            keyword=args.keyword,
            limit=args.limit,
        )
        from rich.table import Table
        table = Table(title=f"书籍列表 (显示 {len(books)} 本)", show_header=True)
        table.add_column("#", justify="right", style="dim")
        table.add_column("标题", style="cyan", max_width=40)
        table.add_column("作者", style="green", max_width=20)
        table.add_column("分类", style="yellow")
        table.add_column("格式", style="blue")
        for i, book in enumerate(books, 1):
            table.add_row(
                str(i),
                book.title,
                book.author,
                book.category,
                ", ".join(book.formats),
            )
        console.print(table)


async def _cmd_status(config: Config) -> None:
    """查看下载状态统计"""
    state = StateDB(config.db_path)
    await state.open()
    try:
        stats = await state.stats()
        total_size = await state.total_size()
        if not stats:
            console.print("[yellow]暂无下载记录[/yellow]")
            return
        print_stats_table(stats, total_size)
    finally:
        await state.close()


async def _cmd_retry(args: argparse.Namespace, config: Config) -> None:
    """重试失败项"""
    if getattr(args, "no_headless", False):
        config.headless = False

    state = StateDB(config.db_path)
    await state.open()
    try:
        failed = await state.get_failed()
        skipped = await state.get_by_status(DownloadStatus.SKIPPED)
        total = len(failed) + len(skipped)
        if not total:
            console.print("[green]没有失败或跳过的下载记录[/green]")
            return

        console.print(
            f"[bold]找到 {len(failed)} 个失败项 + {len(skipped)} 个跳过项，正在重置...[/bold]"
        )
        count = await state.reset_failed()
        console.print(f"[green]已重置 {count} 个记录为待下载状态[/green]")
        console.print("[dim]请运行 download 命令以重新下载[/dim]")
    finally:
        await state.close()


async def _cmd_fetch_data(config: Config) -> None:
    """下载数据源"""
    path = await fetch_catalog(config)
    console.print(f"[green]数据已保存至: {path}[/green]")
