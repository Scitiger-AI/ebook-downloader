"""数据模型定义"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class DownloadStatus(enum.Enum):
    """下载状态"""
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Book:
    """电子书元数据"""
    title: str
    author: str
    link: str
    category: str
    language: str = "ZH"
    level: str = "Unknown"
    formats: tuple[str, ...] = ()

    @property
    def uid(self) -> str:
        """唯一标识：基于链接的最后路径段"""
        # link 形如 https://url89.ctfile.com/f/xxx?p=8866
        return self.link.split("/")[-1].split("?")[0]

    @classmethod
    def from_dict(cls, data: dict) -> Book:
        return cls(
            title=data.get("title", "").strip(),
            author=data.get("author", "").strip(),
            link=data.get("link", "").strip(),
            category=data.get("category", "").strip(),
            language=data.get("language", "ZH"),
            level=data.get("level", "Unknown"),
            formats=tuple(data.get("formats", [])),
        )


@dataclass
class DownloadRecord:
    """下载记录（对应 SQLite 行）"""
    book_uid: str
    title: str
    author: str
    category: str
    link: str
    status: DownloadStatus = DownloadStatus.PENDING
    file_path: str = ""
    file_size: int = 0
    cdn_url: str = ""
    error_msg: str = ""
    retry_count: int = 0
    created_at: str = ""
    updated_at: str = ""
