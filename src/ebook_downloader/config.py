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
    download_concurrency: int = 5

    # 超时配置（秒）
    download_timeout: int = 300
    browser_timeout: int = 30

    # 重试
    max_retries: int = 3
    retry_backoff: int = 5

    # 浏览器
    headless: bool = True

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
