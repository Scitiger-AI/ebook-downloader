"""HTTP 断点续传下载器 + ZIP 解压"""

from __future__ import annotations

import logging
import os
import zipfile
from pathlib import Path
from typing import Callable

import httpx

from .config import Config

logger = logging.getLogger(__name__)

# 进度回调类型: (downloaded_bytes, total_bytes, chunk_bytes)
ProgressCallback = Callable[[int, int, int], None] | None

# 下载块大小
CHUNK_SIZE = 64 * 1024  # 64KB


class FileTooLargeError(Exception):
    """文件超过大小上限"""


async def download_file(
    url: str,
    dest: Path,
    config: Config,
    progress_cb: ProgressCallback = None,
) -> Path:
    """异步流式下载文件，支持断点续传

    Args:
        url: CDN 下载链接
        dest: 目标文件路径
        config: 应用配置
        progress_cb: 进度回调函数

    Returns:
        下载完成的文件路径
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_file = dest.with_suffix(dest.suffix + ".part")

    # 断点续传：检查 .part 文件已下载大小
    downloaded = part_file.stat().st_size if part_file.exists() else 0

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    if downloaded > 0:
        headers["Range"] = f"bytes={downloaded}-"
        logger.debug("断点续传: %s (已下载 %d 字节)", dest.name, downloaded)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(config.download_timeout, connect=30),
        follow_redirects=True,
    ) as client:
        async with client.stream("GET", url, headers=headers) as response:
            # 处理 Range 响应
            if response.status_code == 416:
                # Range Not Satisfiable — 文件可能已完整
                if part_file.exists():
                    part_file.rename(dest)
                    return dest
                raise httpx.HTTPStatusError(
                    "Range request failed",
                    request=response.request,
                    response=response,
                )

            response.raise_for_status()

            # 获取总大小
            if response.status_code == 206:
                # 部分内容响应
                content_range = response.headers.get("content-range", "")
                total = int(content_range.split("/")[-1]) if "/" in content_range else 0
            else:
                total = int(response.headers.get("content-length", 0))
                # 非 206 响应意味着服务器不支持 Range，需从头开始
                if downloaded > 0:
                    downloaded = 0

            mode = "ab" if response.status_code == 206 else "wb"

            # 文件大小上限检查（仅读 Header，不浪费带宽）
            if config.max_file_size > 0 and total > 0:
                max_bytes = config.max_file_size * 1024 * 1024
                if total > max_bytes:
                    raise FileTooLargeError(
                        f"文件大小 {_format_size(total)} 超过上限 {config.max_file_size}MB"
                    )

            with open(part_file, mode) as f:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total, len(chunk))

    # 下载完成，重命名
    part_file.rename(dest)
    logger.info("下载完成: %s (%s)", dest.name, _format_size(downloaded))
    return dest


def _format_size(size: int) -> str:
    """格式化文件大小"""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


# ZIP 内的电子书扩展名
EBOOK_EXTENSIONS = {".epub", ".azw3", ".mobi", ".pdf"}

# ZIP 内需要丢弃的文件
JUNK_EXTENSIONS = {".url", ".txt"}


def _decode_zip_filename(raw: str) -> str:
    """修复 ZIP 内 GBK 编码的文件名

    Windows 中文环境打包的 ZIP 文件名以 GBK 编码存储，
    Python zipfile 默认按 CP437 解码，导致乱码。
    """
    try:
        return raw.encode("cp437").decode("gbk")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return raw


def extract_ebook(
    zip_path: Path,
    book_title: str,
    formats: list[str],
    keep_zip: bool = False,
) -> list[Path]:
    """从 ZIP 中提取电子书文件

    Args:
        zip_path: ZIP 文件路径
        book_title: 书籍标题（用于重命名提取的文件）
        formats: 需要提取的格式列表，如 ["epub", "azw3"]
        keep_zip: 解压后是否保留 ZIP 文件

    Returns:
        提取出的文件路径列表
    """
    if not zip_path.exists() or not zipfile.is_zipfile(zip_path):
        logger.warning("非有效 ZIP 文件，跳过解压: %s", zip_path.name)
        return []

    target_exts = {"." + fmt.lower().lstrip(".") for fmt in formats}
    dest_dir = zip_path.parent
    extracted: list[Path] = []

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue

            decoded_name = _decode_zip_filename(info.filename)
            ext = os.path.splitext(decoded_name)[1].lower()

            # 只提取目标格式的电子书
            if ext not in target_exts:
                continue

            # 用书籍标题 + 原始扩展名作为最终文件名
            # sanitize_filename 由调用方保证 book_title 已清理
            final_name = book_title + ext
            final_path = dest_dir / final_name

            # 避免重复解压
            if final_path.exists():
                logger.debug("文件已存在，跳过: %s", final_name)
                extracted.append(final_path)
                continue

            # 提取到临时名然后重命名
            data = zf.read(info.filename)
            final_path.write_bytes(data)
            extracted.append(final_path)
            logger.debug("解压: %s → %s", decoded_name, final_name)

    if extracted:
        logger.info(
            "解压完成: %s → %s",
            zip_path.name,
            ", ".join(p.name for p in extracted),
        )
        if not keep_zip:
            zip_path.unlink()
            logger.debug("已删除 ZIP: %s", zip_path.name)
    else:
        logger.warning("ZIP 中未找到目标格式 (%s): %s", ", ".join(formats), zip_path.name)

    return extracted
