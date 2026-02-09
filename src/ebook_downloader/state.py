"""SQLite 状态持久化"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .models import Book, DownloadRecord, DownloadStatus

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS download_records (
    book_uid    TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    author      TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    link        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    file_path   TEXT NOT NULL DEFAULT '',
    file_size   INTEGER NOT NULL DEFAULT 0,
    cdn_url     TEXT NOT NULL DEFAULT '',
    error_msg   TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_status ON download_records(status);
CREATE INDEX IF NOT EXISTS idx_category ON download_records(category);
"""


class StateDB:
    """异步 SQLite 状态管理"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.debug("数据库已打开: %s", self.db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("数据库未打开，请先调用 open()")
        return self._db

    async def upsert(self, record: DownloadRecord) -> None:
        """插入或更新下载记录"""
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            INSERT INTO download_records
                (book_uid, title, author, category, link, status,
                 file_path, file_size, cdn_url, error_msg, retry_count,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(book_uid) DO UPDATE SET
                status = excluded.status,
                file_path = excluded.file_path,
                file_size = excluded.file_size,
                cdn_url = excluded.cdn_url,
                error_msg = excluded.error_msg,
                retry_count = excluded.retry_count,
                updated_at = excluded.updated_at
            """,
            (
                record.book_uid, record.title, record.author,
                record.category, record.link, record.status.value,
                record.file_path, record.file_size, record.cdn_url,
                record.error_msg, record.retry_count,
                record.created_at or now, now,
            ),
        )
        await self.db.commit()

    async def get(self, book_uid: str) -> DownloadRecord | None:
        """查询单条记录"""
        cursor = await self.db.execute(
            "SELECT * FROM download_records WHERE book_uid = ?", (book_uid,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    async def get_by_status(self, status: DownloadStatus) -> list[DownloadRecord]:
        """按状态查询记录"""
        cursor = await self.db.execute(
            "SELECT * FROM download_records WHERE status = ?", (status.value,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_record(r) for r in rows]

    async def get_completed_uids(self) -> set[str]:
        """获取所有已完成的 book_uid 集合，用于快速跳过"""
        cursor = await self.db.execute(
            "SELECT book_uid FROM download_records WHERE status = ?",
            (DownloadStatus.COMPLETED.value,),
        )
        rows = await cursor.fetchall()
        return {row["book_uid"] for row in rows}

    async def stats(self) -> dict[str, int]:
        """统计各状态数量"""
        cursor = await self.db.execute(
            "SELECT status, COUNT(*) as cnt FROM download_records GROUP BY status"
        )
        rows = await cursor.fetchall()
        return {row["status"]: row["cnt"] for row in rows}

    async def total_size(self) -> int:
        """已下载总大小（字节）"""
        cursor = await self.db.execute(
            "SELECT COALESCE(SUM(file_size), 0) as total FROM download_records WHERE status = ?",
            (DownloadStatus.COMPLETED.value,),
        )
        row = await cursor.fetchone()
        return row["total"] if row else 0

    async def get_failed(self) -> list[DownloadRecord]:
        """获取所有失败的记录"""
        return await self.get_by_status(DownloadStatus.FAILED)

    async def reset_failed(self) -> int:
        """将所有失败记录重置为 pending，返回影响行数"""
        cursor = await self.db.execute(
            "UPDATE download_records SET status = ?, error_msg = '', retry_count = 0 WHERE status = ?",
            (DownloadStatus.PENDING.value, DownloadStatus.FAILED.value),
        )
        await self.db.commit()
        return cursor.rowcount

    @staticmethod
    def _row_to_record(row: aiosqlite.Row) -> DownloadRecord:
        return DownloadRecord(
            book_uid=row["book_uid"],
            title=row["title"],
            author=row["author"],
            category=row["category"],
            link=row["link"],
            status=DownloadStatus(row["status"]),
            file_path=row["file_path"],
            file_size=row["file_size"],
            cdn_url=row["cdn_url"],
            error_msg=row["error_msg"],
            retry_count=row["retry_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
