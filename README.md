# ebook-downloader

城通网盘电子书批量下载工具。

数据源来自 [jbiaojerry/ebook-treasure-chest](https://github.com/jbiaojerry/ebook-treasure-chest)，收录 **24,071 本**电子书。

## 工作原理

```
Playwright 打开城通网盘页面
  → 拦截 get_file_url.php API 响应
  → 提取 CDN 直链（tv002.com）
  → httpx 异步流式下载 ZIP
  → 自动解压提取电子书（epub/azw3/mobi）
  → 删除 ZIP，按分类目录整理
```

浏览器仅用于获取 CDN 链接（约 3-5 秒），获取后立即释放；实际下载由 HTTP 客户端完成，互不阻塞。

## 环境要求

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)（推荐）或 pip

## 安装

```bash
cd ~/ebook-downloader

# 创建虚拟环境并安装依赖
uv venv && uv pip install -e .

# 安装 Chromium 浏览器（Playwright 需要）
.venv/bin/playwright install chromium
```

## 快速开始

```bash
# 1. 下载书籍数据源（首次使用必须执行）
python -m ebook_downloader fetch-data

# 2. 查看有哪些分类
python -m ebook_downloader list --categories

# 3. 下载指定分类的书籍（自动解压为 epub）
python -m ebook_downloader download -c AI
```

> 所有命令需在 `~/ebook-downloader` 目录下，且激活虚拟环境（`source .venv/bin/activate`）后执行。
> 或直接使用 `.venv/bin/python -m ebook_downloader`。

## 命令详解

### `fetch-data` — 下载/更新数据源

从 GitHub 拉取 `all-books.json`，保存至 `data/` 目录。

```bash
python -m ebook_downloader fetch-data
```

### `list` — 浏览书籍目录

```bash
# 列出所有分类及数量
python -m ebook_downloader list --categories

# 列出指定分类的书籍（默认显示 20 条）
python -m ebook_downloader list -c AI

# 按关键词搜索（匹配标题和作者）
python -m ebook_downloader list -k "李开复"

# 显示更多条目
python -m ebook_downloader list -c 文学 -n 50
```

### `download` — 下载电子书

```bash
# 下载整个分类（默认提取 epub 格式）
python -m ebook_downloader download -c AI

# 下载多个分类
python -m ebook_downloader download -c 文学 历史

# 限制下载数量
python -m ebook_downloader download -c 科幻 -n 10

# 按关键词下载
python -m ebook_downloader download -k "三体"

# 指定提取格式（逗号分隔）
python -m ebook_downloader download -c AI --formats epub,azw3

# 解压后保留原始 ZIP 文件
python -m ebook_downloader download -c AI --keep-zip

# 调整浏览器并发数（默认 3）
python -m ebook_downloader download -c AI --concurrent 5

# 显示浏览器窗口（调试用）
python -m ebook_downloader download -c AI -n 1 --no-headless
```

下载完成后，ZIP 自动解压为电子书文件，按分类存放在 `downloads/{分类名}/` 目录下：

```
downloads/AI/
├── 智慧未来.epub
├── 错觉：AI如何通过数据挖掘误导我们.epub
└── ...
```

#### 格式说明

每本书的 ZIP 包含三种格式（epub / azw3 / mobi），默认只提取 **epub**：

| 格式 | 说明 | 适用场景 |
|------|------|----------|
| epub | 开放标准 | Apple Books、Calibre、Kobo、多看等 |
| azw3 | Kindle 格式 | Kindle 设备 |
| mobi | Kindle 旧格式 | 旧版 Kindle |

通过 `--formats` 按需选择，如 `--formats epub,azw3`。

### `status` — 查看下载统计

```bash
python -m ebook_downloader status
```

输出示例：

```
         下载统计
┏━━━━━━━━━━━━━━━┳━━━━━━━━┓
┃ 状态          ┃   数量 ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━┩
│ ✅ 已完成     │      2 │
│ ──────────    │ ────── │
│ 📚 总计       │      2 │
│ 💾 已下载大小 │ 3.6 MB │
└───────────────┴────────┘
```

### `retry` — 重试失败项

将所有失败记录重置为待下载状态，之后再执行 `download` 即可重新下载。

```bash
python -m ebook_downloader retry
python -m ebook_downloader download -c AI  # 重新下载
```

### 全局选项

```bash
# 启用详细日志（放在子命令之前）
python -m ebook_downloader -v download -c AI

# 指定配置文件
python -m ebook_downloader -C /path/to/config.yaml download -c AI
```

## 配置

复制 `config.example.yaml` 为 `config.yaml` 即可自定义：

```yaml
download_dir: downloads        # 下载目录
browser_concurrency: 3         # 浏览器并发 Context 数
download_timeout: 300           # 单文件下载超时（秒）
browser_timeout: 30             # 浏览器操作超时（秒）
max_retries: 3                  # 失败重试次数
headless: true                  # 是否隐藏浏览器窗口
extract_formats:                # 解压提取的格式
  - epub
keep_zip: false                 # 解压后是否保留 ZIP
```

不创建配置文件时，所有选项使用默认值。

## 断点续传

- 下载进度通过 SQLite（`data/state.db`）持久化
- 中断进程后重启，自动跳过已完成的书籍
- 正在下载的文件以 `.part` 后缀保存，支持 HTTP Range 续传

## 目录结构

```
~/ebook-downloader/
├── data/
│   ├── all-books.json       # 书籍数据源（24,071 条）
│   └── state.db             # 下载状态数据库
├── downloads/               # 电子书库，按分类子目录存放
│   ├── AI/
│   │   ├── 智慧未来.epub
│   │   └── ...
│   ├── 文学/
│   └── ...
└── logs/
    └── ebook-downloader.log # 运行日志
```
