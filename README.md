# 115 网盘管理器

一个运行在本地的 115 网盘管理工具，用来整理海量资源：浏览目录、批量扫描、一键转存、查重清理、AI 自动分类、标签管理。

所有数据（目录树、Cookie、备份）都存在你自己机器上，不上传任何第三方服务器。

---

## 功能特性

### 🗂️ 目录浏览
- 两种浏览模式：**本地数据**（读已扫描入库的数据，速度快）/ **在线浏览**（实时读取云端目录，需要网络）
- 目录树无限层级展开，显示子项数量；支持按时间/名称排序
- 点击目录或文件打开**详情面板**：查看 ID、大小、标签，并可扫描、新建子目录、删除、获取下载直链
- 勾选多项后底部弹出批量工具栏：移动 / 打标签 / 删除

### 🔍 扫描管理
- 把云端目录结构同步到本地 SQLite 数据库，供搜索、查重、AI 分类使用
- 串行扫描，每个目录间隔 3–5 秒，避免触发 115 风控
- **断点续扫**：已扫过的目录直接跳过，中断后继续不用从头来
- 失败自动重试：等待 300 秒后探测网络与 Cookie，通过则自动续扫，最多 3 轮
- 任务可暂停 / 继续 / 停止 / 删除；支持**定时扫描**（指定时间 + 目标目录）
- 支持**强制重扫**（清掉该目录的已扫记录，整棵子树重新拉取）

### 📥 自动转存
- 批量转存 115 分享链接到自己的网盘
- 支持粘贴链接，或上传 `txt` / `csv` / `xlsx` 文件批量解析
- 自动跳过重复链接，记录失效链接便于后续处理
- 每条间隔 4–6 秒防风控

### 🔁 结果与查重
- 按**目录名规范化**聚类：剥掉 `2160p`/`1080p`/`WEB-DL`/`BluRay`/`Remux`/`HEVC`/`x265`/`DDP5.1`/`Atmos` 等发布后缀后同名即视为一组
- 组内 **精确重复** = 文件数与总大小签名一致（**注意：不含内容哈希校验**）
- 可限定查重范围到任意层级目录（选一级目录即比对它的子目录，可逐层下钻）
- 勾选后一键批量删除多余副本（进 115 回收站，30 天可恢复）

### 🤖 AI 自动分类
- 接入任意 OpenAI 兼容接口（DeepSeek / Kimi / OpenAI 均可）
- 为每个目录建议「分类」和「载体」，可选接入 TMDB 补充国家、年份等上下文
- 支持试跑若干条核对准确率后再全量分析
- 审核通过的建议可自动落成标签，并按分类生成移动计划

### 🏷️ 标签管理
- **58 个预设标签**，分 8 类（详见下方「预设标签」）
- 三种打标来源：手动打标 / 规则引擎 / AI 落标
- 规则引擎内置 18 条名字正则 + 1 条碟片结构规则，支持试跑预览
- 移动目录时标签自动跟随；删除目录时同步清理关联

### 💾 备份恢复
- 启动时自动备份一次，之后每日凌晨 3:00–3:05 自动备份
- 周一至周六产出每日备份（保留最近 7 份），周日升级为每周备份（保留最近 4 份）
- 备份数据库的同时复制一份 Cookie
- 支持手动创建备份；恢复前会自动再做一次保护性备份

---

## 技术栈

| 层次 | 技术 |
|---|---|
| 后端 | Python 3.9+ / FastAPI |
| 前端 | 原生 HTML + CSS + JavaScript（无框架、无构建） |
| 数据库 | SQLite（WAL 模式，本地存储） |
| 网络请求 | curl_cffi（模拟 Chrome TLS 指纹） |
| 定时任务 | APScheduler |

---

## 安装与运行

### 环境要求

- Python 3.9 或更高版本
- Windows / macOS / Linux

### Windows

1. 安装 [Python](https://www.python.org/downloads/)，安装时**勾选 "Add Python to PATH"**
2. 双击 `启动115管理器.bat`

脚本会自动完成：检测 8765 端口是否已在运行（已运行则只打开浏览器）→ 查找 `py` 或 `python` → 依赖缺失时自动 `pip install -r requirements.txt` → 启动服务并自动打开浏览器。

### macOS / Linux

```bash
# 首次：一键安装（检查 Python 版本、安装依赖、生成配置文件）
chmod +x install.sh && ./install.sh

# 日常启动
chmod +x start.sh && ./start.sh
```

### 手动启动

```bash
pip install -r requirements.txt
python server.py
```

也可以用 `python3 run.py`，它会自动检查环境、安装依赖再启动服务。

> 依赖安装慢或失败时，可换用国内镜像：
> ```bash
> pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
> ```

### 访问界面

- 本机：<http://127.0.0.1:8765>
- 局域网设备（手机/平板）：`http://本机IP:8765`，启动时会打印本机 IP

### 首次配置 Cookie

1. 用 Chrome / Edge 打开 <https://115.com> 并登录
2. 按 `F12` 打开开发者工具 → 切到 **Network**
3. 点击页面触发请求，找到任意请求的 **Request Headers → Cookie**
4. 复制整行 Cookie 值（需包含 `UID` 与 `USERSESSIONID` 字段）
5. 在项目目录创建 `cookie115.txt`，把 Cookie 粘贴进去（只放这一行）

配置正确后，页面右上角会显示「● Cookie 正常」。

---

## 配置说明

### 环境变量（`.env`）

复制 `.env.example` 为 `.env` 放在**项目目录下**（也兼容放在其上一级目录），按需修改：

```env
# 服务配置
PORT=8765
HOST=0.0.0.0

# 数据库配置
# TREE_DB=115_tree.db
# INV_DB=115_inventory.db

# 凭据文件
# COOKIE_FILE=cookie115.txt
# AI_CONFIG=ai_config.json

# 备份配置
# BACKUP_DIR=backups
# BACKUP_KEEP_DAILY=7
# BACKUP_KEEP_WEEKLY=4

# 扫描配置
# SCAN_INTERVAL_MIN=3.0
# SCAN_INTERVAL_MAX=5.0
# SCAN_RETRY_WAIT=300
# SCAN_RETRY_MAX=3

# AI 配置
# AI_BATCH_SIZE=10
# AI_CALL_DELAY_MIN=1.0
# AI_CALL_DELAY_MAX=2.0
```

完整变量与默认值：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8765` | 服务端口 |
| `HOST` | `0.0.0.0` | 监听地址（`0.0.0.0` 允许局域网访问） |
| `TREE_DB` | `115_tree.db` | 主数据库路径 |
| `INV_DB` | `115_inventory.db` | 预留，当前未使用 |
| `COOKIE_FILE` | `cookie115.txt` | Cookie 文件路径 |
| `AI_CONFIG` | `ai_config.json` | AI 配置文件路径 |
| `BACKUP_DIR` | `backups` | 备份目录 |
| `BACKUP_KEEP_DAILY` | `7` | 每日备份保留份数 |
| `BACKUP_KEEP_WEEKLY` | `4` | 每周备份保留份数 |
| `SCAN_INTERVAL_MIN` | `3.0` | 扫描最小间隔（秒） |
| `SCAN_INTERVAL_MAX` | `5.0` | 扫描最大间隔（秒） |
| `SCAN_RETRY_WAIT` | `300` | 扫描失败后的重试等待（秒） |
| `SCAN_RETRY_MAX` | `3` | 扫描最大自动重试轮数 |
| `AI_BATCH_SIZE` | `10` | AI 每批处理条数 |
| `AI_CALL_DELAY_MIN` | `1.0` | AI 调用最小间隔（秒） |
| `AI_CALL_DELAY_MAX` | `2.0` | AI 调用最大间隔（秒） |

> 路径类变量的相对路径基于**启动服务时的工作目录**解析，因此请在项目目录内启动服务。
> 优先级：环境变量 > `.env` 文件 > 代码默认值。

### AI 配置（`ai_config.json`）

复制 `ai_config.example.json` 为 `ai_config.json`：

```json
{
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-chat",
  "key": "your_api_key_here",
  "batch_size": 10,
  "tmdb_api_key": ""
}
```

| 字段 | 说明 |
|---|---|
| `base_url` | 任意 OpenAI 兼容端点（DeepSeek / Kimi / OpenAI…） |
| `model` | 模型名，如 `deepseek-chat` |
| `key` | API Key（**字段名就是 `key`**） |
| `batch_size` | 每批处理条数，默认 10 |
| `tmdb_api_key` | 可选，用于补充国家/类型/年份上下文 |

也可以直接在网页端「AI 分类」页签的配置卡里填写，点「测试连接」验证。系统提示词与分类词表同样可以在网页端调整。

---

## 使用指南

### 目录浏览

- 顶部切换「💾 本地数据」/「🌐 在线浏览」。未扫描的目录用在线模式也能直接看
- 点击目录展开；点击名称打开右侧详情面板
- 勾选多项后，底部工具栏可批量「移动到…」「打标签」「删除」

### 扫描管理

1. 点「📂 点击选择目录…」选择要扫描的目录（支持任意层级）
2. 需要重新完整拉取时勾选「强制重扫（清旧记录）」
3. 点「开始扫描」，在下方任务列表查看进度
4. 同一页签还可添加「定时扫描」：选目录 + 选时间

扫描速度取决于目录数量，一个几千目录的根目录大约需要几小时。

### 搜索

- 输入关键字（至少 2 个字）回车搜索
- 可叠加筛选：类型（全部/仅目录/仅文件）、根目录、大小范围
- 结果支持直接打开、扫描、删除、下载

### 自动转存

1. 粘贴分享链接（每行一条），或上传 `txt`/`csv`/`xlsx`
2. 解析后核对清单
3. 选择目标目录 → 开始转存

### 结果与查重

1. 选择查重范围（默认「全部一级目录」，也可下钻到任意层级）
2. 查看分组：精确重复 / 模糊重复 / 重复数量
3. 勾选要删除的副本 → 批量删除

### AI 分类

1. 在「AI 配置」卡填写接口信息并测试连接
2. 选择分析范围
3. 先试跑若干条核对准确率，再全量分析
4. 审核通过的建议 → 自动落成标签 / 生成移动计划

### 标签

- 点任意标签查看该标签下的全部资源，勾选后可批量移动
- 手动打标：勾选资源 → 底部「🏷 打标签」→ 选标签 → 应用
- 规则打标：「规则试跑」预览效果 → 「规则打标…」正式执行

### 备份恢复

- 右上角「💾 备份」→「创建备份」手动备份
- 恢复：选择备份 → 恢复（恢复前会自动备份当前数据库）

### 日志

右上角「📜 日志」打开实时日志窗口：支持按来源过滤、自动跟随滚动、清屏，窗口可拖动和拉伸。

### Cookie 管理

点击右上角 Cookie 状态区，可查看当前状态、更新 Cookie、验证有效性。

---

## 预设标签（58 个）

| 类别 | 数量 | 标签 |
|---|---|---|
| 类型 | 8 | 电影、剧集、动漫、纪录片、综艺、演唱会·音乐、音乐、体育 |
| 分辨率 | 13 | 4K、1080P、720P、480P、Remux、Web-DL、HDTV、BluRay、原盘、Encode、HEVC、H.264、10bit |
| 字幕 | 7 | 中字、国语、粤语、日语、英语、双语、多音轨 |
| 国家 | 6 | 华语、日本、韩国、欧美、港台、印度 |
| 画质 | 6 | HDR、HDR10、HDR10+、Dolby Vision、HLG、SDR |
| 音频 | 11 | DTS-HD、DTS-HD MA、TrueHD、Atmos、DTS、DTS-X、AC3、AAC、FLAC、LPCM、DSD |
| 状态 | 4 | 未看、在看、看完可删、待人工 |
| 来源 | 3 | 分享转存、自购、录制 |

规则引擎内置 18 条名字正则 + 1 条结构规则（子级含 `BDMV`/`VIDEO_TS`/`CERTIFICATE` 时，父目录判为「原盘」）。图片、`nfo`、压缩包等非媒体文件会被跳过；逐集文件（`S01`、`S01E02`、`E01`、`EP01`）不参与目录级打标。

---

## 文件结构

```
115manager/
├── server.py              # FastAPI 主服务，提供全部 REST API
├── api115.py              # 115 网盘 API 客户端（curl_cffi 模拟 Chrome 指纹）
├── config.py              # 统一配置（环境变量 / .env）
├── scanner.py             # 目录扫描管理器（后台线程 + 断点续扫）
├── transfer_worker.py     # 转存工作线程
├── ai_worker.py           # AI 分类工作线程
├── tags.py                # 标签系统（58 个预设标签）
├── tag_rules.py           # 标签规则引擎（18 条种子规则）
├── dedup.py               # 查重引擎
├── logbus.py              # 日志总线（环形缓冲 + 增量拉取）
├── tmdb.py                # TMDB API（可选，用于补充影视信息）
├── static/
│   ├── index.html         # 前端页面
│   ├── app.js             # 前端逻辑
│   └── style.css          # 样式表
├── requirements.txt       # Python 依赖
├── .env.example           # 环境变量示例
├── ai_config.example.json # AI 配置示例
├── config_example.py      # 配置项示例（参考用，不被任何模块引用）
├── install.sh             # Linux/macOS 安装脚本
├── start.sh               # Linux/macOS 启动脚本
├── run.py                 # Python 启动脚本（自动检查环境 + 装依赖）
└── 启动115管理器.bat       # Windows 启动脚本
```

运行时会在项目目录下生成（已被 `.gitignore` 排除）：

```
115_tree.db          # 目录树数据库
cookie115.txt        # 登录凭据
ai_config.json       # AI 接口配置
backups/             # 自动备份目录
```

---

## 安全须知

以下文件属于敏感数据，不要外泄、不要提交到公开仓库：

- **`cookie115.txt`** — 你的 115 登录凭据，拿到即可操作你的账号
- **`ai_config.json`** — 含 AI 接口的 API Key
- **`115_tree.db`** — 你的完整网盘目录结构，属于隐私数据
- **`backups/`** — 内含数据库副本和 Cookie 副本

仓库自带的 `.gitignore` 已覆盖以上文件，新增部署时请确认它没有被删掉。

---

## 常见问题

**Q：双击 bat 后闪退 / 提示找不到 Python？**
A：确认 Python 已安装并加入 PATH。打开 cmd 输入 `python --version` 验证。

**Q：依赖安装失败？**
A：换国内镜像重试：
```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```
或手动安装：
```bash
pip install fastapi uvicorn curl_cffi apscheduler openpyxl python-multipart pydantic aiofiles
```

**Q：提示端口被占用 / `Address already in use`？**
A：先查占用进程，再改 `.env` 里的 `PORT`：
```bash
# Windows
netstat -ano | findstr :8765
# macOS / Linux
lsof -i :8765
```

**Q：Cookie 显示「失效」或提示格式无效？**
A：Cookie 必须同时包含 `UID` 和 `USERSESSIONID` 字段。按上面的「首次配置 Cookie」重新获取，注意只粘贴一行、不要断开。

**Q：提示 `database is locked`？**
A：同一个数据库被多个进程打开导致。关闭其他正在运行的实例后重启；极端情况下可删除 `115_tree.db` 重新扫描（会丢失已扫描数据）。

**Q：AI 提示「配置未填写完整(base_url/model/key)」？**
A：`ai_config.json` 里的密钥字段名必须是 `key`（不是 `api_key`）。建议直接用网页端「AI 配置」卡填写并测试连接。

**Q：AI 分类结果不准？**
A：先用小批量试跑核对；调整系统提示词与分类词表；换用更强的模型；个别结果可手动修正。

**Q：扫描很慢？**
A：扫描是串行的，每目录间隔 3–5 秒，这是为了不触发 115 风控。支持断点续扫，中断后继续即可。若确实需要提速，可调小 `SCAN_INTERVAL_MIN/MAX`（有风控风险）。

**Q：被 115 风控了怎么办？**
A：程序会自动冷却重试（触发后冷却 600 秒）。若持续失败，暂停操作等待一段时间再试。

**Q：删除的文件能恢复吗？**
A：删除是移入 115 回收站，30 天内可在网盘手动恢复。

**Q：想换台电脑用？**
A：把整个项目文件夹拷过去（含数据库、配置、凭据），新电脑装好 Python 后双击 bat 即可。

---

## 开发说明

### 架构

```
前端层   Browser: index.html + app.js + style.css
   ↓ REST / 静态文件
API 层   FastAPI (server.py)：路由 + 静态文件 + 定时调度
   ↓
业务层   scanner / transfer_worker / ai_worker / tags / tag_rules / dedup
   ↓
基础层   api115（115 API 客户端） / config（配置） / logbus（日志）
   ↓
数据层   115_tree.db / cookie115.txt / ai_config.json
```

### 数据流

**扫描**：选择目录 → `server.py` 提交任务 → `scanner.py` 逐目录调用 `api115.py` → 解析目录结构 → 写入 `tree_nodes`/`scan_state` → `logbus` 记日志

**转存**：粘贴链接 → `server.py` 解析 → `transfer_worker.py` → `api115.py` 转存接口 → 同步写入 `tree_nodes` → `logbus` 记日志

**AI 分类**：选择范围 → `server.py` 提交批次 → `ai_worker.py` 分批调用外部 AI 接口（可选叠加 TMDB 信息）→ 解析结果写入 `ai_suggestions` → 审核通过后落成标签 → `logbus` 记日志

### 目录选择器

全站 8 处目录选择（查重范围、AI 分析范围、转存目标、批量移动目标、发起扫描、定时扫描、搜索范围、AI 移动父目录）统一复用 `app.js` 中的 `pickDir()` 组件，提供一致的能力：

```js
const sel = await pickDir({title, cid, name, persistKey, allowAll, allLabel, allCid, withFilter});
// → {cid, name, mode, exists, scan_state}；取消返回 null
```

- 本地数据 / 在线浏览双模式、排序、**展开状态记忆**（按 `persistKey` 隔离）
- 选中未入库目录时会明确提示（依赖本地库的功能可能得到空结果）
- 可选「全部」项（`allowAll` + `allCid`）与「过滤 + 搜本地库」（`withFilter`）

### API 端点

共 69 个路由，按模块分组：

| 分组 | 数量 | 主要端点 |
|---|---|---|
| 系统 | 3 | `/api/health`、`/api/dashboard`、`/api/logs` |
| 目录 | 6 | `/api/roots`、`/api/tree/{cid}`、`/api/node/{cid}`、`/api/live/{cid}`、`/api/nodes/tags` |
| 搜索 | 4 | `/api/search`、`/api/search/history` |
| 扫描 | 6 | `/api/scan/start`、`/api/scan/jobs`、`/api/scan/pause\|resume\|stop/{jid}` |
| 定时 | 3 | `/api/schedules` |
| 转存 | 6 | `/api/transfer/parse/text`、`/api/transfer/parse/file`、`/api/transfer/task`、`/api/transfer/tasks` |
| 文件操作 | 5 | `/api/fs/mkdir`、`/api/fs/move`、`/api/fs/delete`、`/api/fs/delete/batch`、`/api/fs/download/{cid}` |
| AI | 14 | `/api/ai/config`、`/api/ai/config/test`、`/api/ai/analyze`、`/api/ai/batches`、`/api/ai/suggestions`、`/api/ai/plan`、`/api/ai/move-history` |
| 标签 | 14 | `/api/tags`、`/api/tags/apply`、`/api/tags/rules`、`/api/tags/rules/run`、`/api/tags/cleanup` |
| 查重 | 1 | `/api/dups` |
| 备份 | 3 | `/api/backup`、`/api/backups`、`/api/backups/restore/{name}` |
| Cookie | 3 | `/api/cookie`、`/api/cookie/test` |

### 数据库表（16 张）

`tree_nodes`（目录树）、`scan_state`（断点续扫状态）、`scan_jobs`（扫描任务）、`schedules`（定时扫描）、`tags` / `node_tags` / `tag_rules`（标签体系）、`transfer_tasks` / `transfer_items`（转存任务）、`ai_batches` / `ai_suggestions` / `ai_move_history`（AI 分类）、`app_settings`（应用设置）、`search_history`（搜索历史）、`stats_history`（统计快照）、`cookie_meta`（Cookie 元信息）

### 防风控机制

- `api115.py` 使用 curl_cffi 的 `impersonate="chrome124"` 模拟 Chrome TLS 指纹
- 检测到 WAF 拦截后自动冷却 **600 秒**再重试
- 各模块自带请求间隔：扫描 3–5 秒/目录、转存 4–6 秒/条、AI 调用 1–2 秒/条
- 扫描任务全局串行，避免并发压垮接口

### 日志实现

`logbus.py` 使用内存环形缓冲（2000 条，`seq` 单调递增作游标），前端通过 `GET /api/logs?after=<seq>` 每 2 秒增量拉取。**不是 WebSocket。**

### 扩展开发

1. **添加 API**：在 `server.py` 中新增路由
2. **改前端**：直接编辑 `static/` 下的 HTML/JS/CSS，无需构建
3. **换 AI 提供商**：任意 OpenAI 兼容端点改 `base_url` 即可；特殊协议改 `ai_worker.py`
4. **自定义标签规则**：改 `tag_rules.py` 的正则，或通过网页端「规则试跑」调整

---

## 许可证

本项目仅供个人学习使用，请勿用于商业用途。

## 更新日志

### v1.0.0 (2026-09-14)
- 初始版本发布
- 支持目录浏览、扫描、转存、查重
- 支持 AI 自动分类
- 支持标签管理
- 支持备份恢复
