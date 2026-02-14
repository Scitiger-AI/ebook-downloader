"""代理池管理模块

从代理池 API 批量获取代理，经过并发可用性验证后按响应时间排序使用。
兼容快代理（批量文本）和单代理 JSON API 两种格式。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# 两次 API 拉取之间的最小间隔（秒）
_MIN_FETCH_INTERVAL = 5.0

# 代理验证参数
_VERIFY_CONCURRENCY = 50     # 并发验证数
_VERIFY_TIMEOUT = 5.0        # 单个代理验证超时（秒）
_VERIFY_TARGET = "https://url89.ctfile.com"  # 验证目标（实际业务站点）


class ProxyPool:
    """代理批量获取、验证与本地轮换

    支持两种数据源：
    1. API 模式：从代理池 API 批量拉取，队列耗尽时自动拉新
    2. 文件模式：从本地 proxy.txt 一次性加载，用完即止

    设计要点：
    - 并发验证后按响应时间排序入队
    - get_proxy() 返回当前代理；invalidate() 淘汰当前代理并从队列取下一个
    - 黑名单防止重复使用已失效代理
    - asyncio.Lock 防止并发拉取（thundering herd）
    - 不可用时 graceful fallback 为直连（返回 None）
    """

    def __init__(
        self,
        api_url: str = "",
        proxy_file: str | Path = "",
    ) -> None:
        self._api_url = api_url
        self._proxy_file = Path(proxy_file) if proxy_file else None
        self._current_proxy: str | None = None
        self._queue: deque[str] = deque()        # 待使用的代理队列（已验证、按速度排序）
        self._blacklist: set[str] = set()         # 本次运行中已失效的代理
        self._lock = asyncio.Lock()
        self._last_fetch_time: float = 0.0
        self._file_round: int = 0             # 文件模式：当前第几轮
        self._file_exhausted: bool = False     # 文件模式：验证后 0 可用，彻底放弃

    async def get_proxy(self) -> str | None:
        """获取当前可用代理，无可用时自动轮换或拉取"""
        if self._current_proxy:
            return self._current_proxy

        async with self._lock:
            # 双重检查
            if self._current_proxy:
                return self._current_proxy
            return await self._pick_next()

    async def invalidate(self) -> None:
        """标记当前代理失效，加入黑名单，自动切换到下一个"""
        async with self._lock:
            old = self._current_proxy
            self._current_proxy = None
            if old:
                self._blacklist.add(old)
                logger.info(
                    "代理已加入黑名单 (黑名单: %d, 队列剩余: %d): %s",
                    len(self._blacklist), len(self._queue), old,
                )

    async def _pick_next(self) -> str | None:
        """从队列中取出下一个可用代理，队列空时按模式补充"""
        proxy = self._dequeue_valid()
        if proxy:
            self._current_proxy = proxy
            logger.info("切换代理: %s (队列剩余: %d)", proxy, len(self._queue))
            return proxy

        # 队列耗尽，尝试补充
        await self._refill()

        proxy = self._dequeue_valid()
        if proxy:
            self._current_proxy = proxy
            logger.info("切换代理: %s (队列剩余: %d)", proxy, len(self._queue))
            return proxy

        logger.warning("无可用代理，回退为直连")
        return None

    async def _refill(self) -> None:
        """根据数据源模式补充代理队列"""
        if self._proxy_file:
            await self._load_from_file()
        elif self._api_url:
            await self._fetch_from_api()

    def _dequeue_valid(self) -> str | None:
        """从队列头部取出第一个不在黑名单中的代理"""
        while self._queue:
            candidate = self._queue.popleft()
            if candidate not in self._blacklist:
                return candidate
        return None

    async def _load_from_file(self) -> None:
        """从本地 proxy.txt 加载代理，支持多轮复用

        首轮：加载文件 → 去除黑名单 → 验证 → 入队
        后续轮次：清空黑名单 → 重新验证全部代理 → 真正过期的被验证淘汰
        验证后 0 个可用 → 放弃，不再重试
        """
        if self._file_exhausted:
            return

        if not self._proxy_file or not self._proxy_file.exists():
            logger.warning("代理文件不存在: %s", self._proxy_file)
            self._file_exhausted = True
            return

        # 读取文件（每轮都读，支持运行中手动更新文件内容）
        text = self._proxy_file.read_text(encoding="utf-8")
        proxies = _parse_proxy_list(text)

        if not proxies:
            logger.warning("代理文件为空: %s", self._proxy_file)
            self._file_exhausted = True
            return

        self._file_round += 1

        if self._file_round > 1:
            # 非首轮：清空黑名单，给所有代理重新验证的机会
            old_blacklist_size = len(self._blacklist)
            self._blacklist.clear()
            logger.info(
                "文件代理第 %d 轮复用: 清空黑名单 (%d 个), 重新验证全部 %d 个代理",
                self._file_round, old_blacklist_size, len(proxies),
            )

        candidates = [p for p in proxies if p not in self._blacklist]
        logger.info(
            "文件代理加载 (第 %d 轮): 共 %d 个, 待验证 %d 个",
            self._file_round, len(proxies), len(candidates),
        )

        if not candidates:
            self._file_exhausted = True
            return

        # 并发验证
        verified = await _verify_proxies(candidates)

        if not verified:
            logger.warning("文件代理全部验证失败，放弃重试")
            self._file_exhausted = True
            return

        self._queue.extend(verified)
        logger.info(
            "文件代理验证完成 (第 %d 轮): %d/%d 可用, 队列总计 %d 个",
            self._file_round, len(verified), len(candidates), len(self._queue),
        )

    async def _fetch_from_api(self) -> None:
        """从代理池 API 拉取一批代理，验证后按响应时间排序入队"""
        elapsed = time.monotonic() - self._last_fetch_time
        if elapsed < _MIN_FETCH_INTERVAL:
            await asyncio.sleep(_MIN_FETCH_INTERVAL - elapsed)

        self._last_fetch_time = time.monotonic()

        try:
            # proxy=None: 绕过系统代理（macOS Surge/Clash），直连代理池 API
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15),
                proxy=None,
            ) as client:
                resp = await client.get(self._api_url)

                if resp.status_code != 200:
                    logger.warning("代理 API 返回 HTTP %d", resp.status_code)
                    return

                proxies = _parse_proxy_response(resp.text)
                # 过滤黑名单
                candidates = [p for p in proxies if p not in self._blacklist]
                logger.info(
                    "拉取代理: 共 %d 个, 去除黑名单后 %d 个待验证",
                    len(proxies), len(candidates),
                )

        except Exception as e:
            logger.warning("代理 API 请求失败: %s", e)
            return

        if not candidates:
            return

        # 并发验证
        verified = await _verify_proxies(candidates)
        self._queue.extend(verified)
        logger.info(
            "代理验证完成: %d/%d 可用, 队列总计 %d 个",
            len(verified), len(candidates), len(self._queue),
        )


async def _verify_proxies(proxies: list[str]) -> list[str]:
    """并发验证代理可用性，返回按响应时间升序排列的可用代理列表"""
    semaphore = asyncio.Semaphore(_VERIFY_CONCURRENCY)

    async def check_one(proxy_url: str) -> tuple[str, float] | None:
        async with semaphore:
            return await _test_proxy(proxy_url)

    tasks = [check_one(p) for p in proxies]
    results = await asyncio.gather(*tasks)

    # 过滤成功的，按响应时间排序
    valid: list[tuple[str, float]] = [r for r in results if r is not None]
    valid.sort(key=lambda x: x[1])

    if valid:
        logger.info(
            "最快代理: %s (%.1fms), 最慢: %s (%.1fms)",
            valid[0][0], valid[0][1] * 1000,
            valid[-1][0], valid[-1][1] * 1000,
        )

    return [proxy_url for proxy_url, _ in valid]


async def _test_proxy(proxy_url: str) -> tuple[str, float] | None:
    """测试单个代理是否可用

    通过代理向目标站点发送 HEAD 请求，验证 HTTPS 隧道（CONNECT）是否可用。

    Returns:
        (proxy_url, response_time) 或 None（不可用）
    """
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_VERIFY_TIMEOUT),
            proxy=proxy_url,
        ) as client:
            resp = await client.head(_VERIFY_TARGET)
            elapsed = time.monotonic() - start
            # 任何非连接级错误的响应都说明隧道可用（即使是 403/404 等）
            if resp.status_code < 500:
                return (proxy_url, elapsed)
            return None
    except Exception:
        return None


def _parse_proxy_list(text: str) -> list[str]:
    """解析纯文本代理列表（每行一个 ip:port）"""
    result = []
    for line in text.splitlines():
        line = line.strip()
        if line and ":" in line and not line.startswith("#"):
            p = _normalize_proxy(line)
            if p:
                result.append(p)
    return result


def _parse_proxy_response(text: str) -> list[str]:
    """解析代理 API 响应，兼容多种格式，统一返回代理列表

    支持格式：
    1. 快代理批量文本: 每行一个 ip:port
    2. JSON 单代理: {"proxy": "ip:port"} / {"ip": "x", "port": y}
    3. JSON 列表: ["ip:port", ...]
    """
    text = text.strip()
    if not text:
        return []

    # 尝试 JSON 解析
    try:
        data = json.loads(text)

        # JSON 列表
        if isinstance(data, list):
            result = []
            for item in data:
                p = _normalize_proxy(str(item)) if isinstance(item, str) else None
                if isinstance(item, dict):
                    p = _extract_proxy_from_dict(item)
                if p:
                    result.append(p)
            return result

        # JSON 单对象
        if isinstance(data, dict):
            p = _extract_proxy_from_dict(data)
            return [p] if p else []

        # JSON 字符串
        if isinstance(data, str):
            p = _normalize_proxy(data)
            return [p] if p else []

    except (json.JSONDecodeError, ValueError):
        pass

    # 纯文本格式：每行一个 ip:port（快代理格式）
    result = []
    for line in text.splitlines():
        line = line.strip()
        if line and ":" in line:
            p = _normalize_proxy(line)
            if p:
                result.append(p)
    return result


def _extract_proxy_from_dict(data: dict) -> str | None:
    """从字典中提取代理地址"""
    if "proxy" in data:
        return _normalize_proxy(str(data["proxy"]))
    if "ip" in data and "port" in data:
        return _normalize_proxy(f"{data['ip']}:{data['port']}")
    for key in ("https", "http", "addr", "address", "server"):
        if key in data:
            return _normalize_proxy(str(data[key]))
    return None


def _normalize_proxy(raw: str) -> str | None:
    """将原始代理字符串标准化为 http://ip:port 格式"""
    raw = raw.strip()
    if not raw:
        return None

    # 已包含协议头
    if raw.startswith(("http://", "https://", "socks5://", "socks4://")):
        return raw

    # 纯 ip:port → 默认 http 协议
    return f"http://{raw}"
