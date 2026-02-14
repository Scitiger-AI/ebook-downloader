"""配置管理模块"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    """应用配置，支持 YAML 文件加载和默认值"""

    # 路径配置
    project_root: Path = field(default_factory=lambda: Path.cwd())
    download_dir: str = "downloads"
    data_dir: str = "data"
    log_dir: str = "logs"

    # 数据源
    data_url: str = (
        "https://raw.githubusercontent.com/jbiaojerry/ebook-treasure-chest"
        "/main/docs/all-books.json"
    )

    # 并发控制
    browser_concurrency: int = 3
    download_concurrency: int = 10  # HTTP 下载并发数（独立于浏览器，仅控制下载阶段）

    # 生产者-消费者队列
    cdn_queue_size: int = 20          # CDN 任务队列容量（防积压导致链接过期）
    max_download_retries: int = 2     # 下载阶段独立重试次数

    # 超时配置（秒）
    download_timeout: int = 300
    browser_timeout: int = 30

    # 重试
    max_retries: int = 3
    retry_backoff: int = 5

    # 浏览器
    headless: bool = True

    # 代理池
    proxy_api_url: str = ""  # 代理池 API 地址，为空则不使用代理

    # 智能延迟（模拟真人行为，避免反爬）
    request_min_delay: float = 5.0   # 两次页面访问之间的最小间隔（秒）
    request_max_delay: float = 15.0  # 两次页面访问之间的最大间隔（秒）
    enable_smart_delay: bool = True  # 是否启用智能延迟

    # 书籍过滤
    exclude_categories: list[str] = field(default_factory=list)  # 排除的分类（如漫画、绘本等大图类）
    max_file_size: int = 500  # 单文件大小上限（MB），超过则跳过，0 表示不限制

    # 解压配置
    extract_formats: list[str] = field(default_factory=lambda: ["epub"])
    keep_zip: bool = False

    @property
    def download_path(self) -> Path:
        p = Path(self.download_dir)
        return p if p.is_absolute() else self.project_root / p

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else self.project_root / p

    @property
    def log_path(self) -> Path:
        p = Path(self.log_dir)
        return p if p.is_absolute() else self.project_root / p

    @property
    def db_path(self) -> Path:
        return self.data_path / "state.db"

    @property
    def catalog_path(self) -> Path:
        return self.data_path / "all-books.json"

    def ensure_dirs(self) -> None:
        """创建必要的目录"""
        self.download_path.mkdir(parents=True, exist_ok=True)
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)


def load_config(config_path: str | Path | None = None) -> Config:
    """加载配置文件，未指定则使用默认值"""
    if config_path is None:
        # 尝试从项目根目录加载
        candidates = ["config.yaml", "config.yml"]
        for name in candidates:
            p = Path.cwd() / name
            if p.exists():
                config_path = p
                break

    if config_path is not None:
        path = Path(config_path)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            # 过滤掉 Config 不接受的字段
            valid_fields = {f.name for f in Config.__dataclass_fields__.values()}
            filtered = {k: v for k, v in data.items() if k in valid_fields}
            return Config(project_root=path.parent, **filtered)

    return Config()
