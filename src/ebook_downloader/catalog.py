"""书籍目录加载与筛选"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from .config import Config
from .models import Book

logger = logging.getLogger(__name__)


class Catalog:
    """书籍目录管理"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._books: list[Book] = []

    @property
    def books(self) -> list[Book]:
        if not self._books:
            self._books = self._load()
        return self._books

    def _load(self) -> list[Book]:
        """从本地 JSON 加载书籍列表"""
        path = self.config.catalog_path
        if not path.exists():
            logger.error("数据文件不存在: %s，请先运行 fetch-data", path)
            return []

        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        books = []
        for item in raw:
            try:
                book = Book.from_dict(item)
                if book.title and book.link:
                    books.append(book)
            except Exception as e:
                logger.warning("解析书籍记录失败: %s", e)

        logger.info("加载 %d 本书籍", len(books))
        return books

    def categories(self) -> dict[str, int]:
        """返回所有分类及对应数量，按数量降序"""
        counts: dict[str, int] = {}
        for book in self.books:
            counts[book.category] = counts.get(book.category, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))

    def filter(
        self,
        categories: list[str] | None = None,
        keyword: str | None = None,
        limit: int | None = None,
    ) -> list[Book]:
        """按分类和/或关键词筛选书籍"""
        result = self.books

        if categories:
            cat_set = set(categories)
            result = [b for b in result if b.category in cat_set]

        if keyword:
            kw = keyword.lower()
            result = [
                b for b in result
                if kw in b.title.lower() or kw in b.author.lower()
            ]

        if limit is not None and limit > 0:
            result = result[:limit]

        return result


async def fetch_catalog(config: Config) -> Path:
    """从 GitHub 下载 all-books.json"""
    config.ensure_dirs()
    target = config.catalog_path

    logger.info("正在从 %s 下载数据...", config.data_url)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(config.data_url)
        resp.raise_for_status()

    target.write_bytes(resp.content)
    # 验证 JSON 有效性
    data = json.loads(resp.content)
    logger.info("下载完成，共 %d 条记录，保存至 %s", len(data), target)
    return target
