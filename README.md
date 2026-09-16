# 115 网盘管理器

一个运行在本地的 115 网盘管理工具，帮你整理海量资源：浏览目录、批量扫描、一键转存、查重清理、AI 自动分类、标签管理。

## 功能特性

### 🗂️ 目录浏览
- 浏览 115 网盘目录结构
- 支持无限展开目录树
- 显示目录大小、文件数量等信息

### 🔍 批量扫描
- 将云端目录信息同步到本地数据库
- 串行扫描（3-5秒/目录），避免触发风控
- 支持断点续扫，失败自动重试

### 📥 一键转存
- 批量转存分享链接到网盘
- 支持粘贴链接或上传 txt/csv/xlsx 文件
- 每条间隔 4-6 秒防风控

### 🔁 查重清理
- 自动分析全库，找出重复资源
- 精确重复和模糊重复分开显示
- 支持一键批量删除多余副本

### 🤖 AI 自动分类
- 使用 AI 识别资源类型（支持 DeepSeek/Kimi/OpenAI）
- 为每个目录建议「类型」和「载体」
- 审核通过的建议自动落成标签

### 🏷️ 标签管理
- 21 个预设标签（类型/属性/状态/来源）
- 支持手动打标、规则打标、AI 落标
- 按标签筛选和批量移动

### 💾 备份恢复
- 自动备份数据库和 Cookie
- 每日凌晨 3 点自动备份
- 保留最近 7 个每日 + 4 个周备份

## 技术栈

- **后端**: Python + FastAPI
- **前端**: 纯 HTML/CSS/JavaScript（无框架）
- **数据库**: SQLite（本地存储）
- **网络请求**: curl_cffi（Chrome TLS 指纹）
- **定时任务**: APScheduler

## 安装与运行

### 环境要求

- Python 3.9+
- Windows/macOS/Linux

### 安装步骤

1. **安装 Python**
   - 下载：https://www.python.org/downloads/
   - 安装时勾选 "Add Python to PATH"

2. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```

3. **配置 Cookie**
   - 用 Chrome/Edge 浏览器打开 https://115.com 并登录
   - 按 F12 打开开发者工具
   - 切换到 Network 标签
   - 点击页面触发请求，找到 Request Headers → Cookie
   - 复制整行 Cookie 值
   - 创建 `cookie115.txt` 文件，粘贴 Cookie 内容

4. **启动服务**
   ```bash
   python server.py
   ```
   或双击 `启动115管理器.bat`（Windows）

5. **访问界面**
   - 浏览器自动打开 `http://127.0.0.1:8765`
   - 局域网设备可通过 `http://本机IP:8765` 访问

## 配置说明

### 环境变量配置

复制 `.env.example` 为 `.env`，按需修改：

```env
# 服务端口
PORT=8765

# 数据库路径
TREE_DB=115_tree.db

# 扫描间隔（秒）
SCAN_INTERVAL_MIN=3.0
SCAN_INTERVAL_MAX=5.0

# 备份设置
BACKUP_KEEP_DAILY=7
BACKUP_KEEP_WEEKLY=4
```

### AI 配置

创建 `ai_config.json` 文件：

```json
{
  "provider": "deepseek",
  "api_key": "your_api_key_here",
  "model": "deepseek-chat",
  "base_url": "https://api.deepseek.com"
}
```

支持的 AI 提供商：
- DeepSeek
- Kimi
- OpenAI 兼容接口

## 文件结构

```
115manager/
├── server.py           # 主服务（FastAPI）
├── api115.py           # 115 网盘 API 客户端
├── config.py           # 配置管理
├── scanner.py          # 目录扫描
├── transfer_worker.py  # 转存工作线程
├── ai_worker.py        # AI 分类工作线程
├── tags.py             # 标签系统
├── tag_rules.py        # 标签规则引擎
├── dedup.py            # 查重功能
├── logbus.py           # 日志系统
├── tmdb.py             # TMDB API（可选）
├── static/
│   ├── index.html      # 前端页面
│   ├── app.js          # 前端逻辑
│   └── style.css       # 样式表
├── requirements.txt    # Python 依赖
└── 启动115管理器.bat   # Windows 启动脚本
```

## 使用指南

### 首次使用

1. 启动服务后，页面右上角显示 Cookie 状态
2. 配置 Cookie 后状态变为「● Cookie 正常」
3. 选择一个根目录进行扫描
4. 扫描完成后即可浏览和搜索

### 核心操作

#### 目录浏览
- 左上角下拉选择根目录
- 点击目录展开浏览
- 勾选目录/文件 → 底部弹出批量工具栏

#### 批量操作
- 📁 **移动到…**：选目标目录，一键批量移动
- 🏷 **打标签**：给选中项打上标签
- 🗑 **删除**：移入 115 回收站（30天可恢复）

#### 搜索
- 输入关键字（≥2字）
- 支持按类型、根目录、大小范围筛选

#### 自动转存
- 粘贴分享链接（每行一条）
- 解析后确认清单
- 选择目标目录 → 开始转存

### AI 分类

1. 配置 AI 接口（ai_config.json）
2. 选择范围 → 试跑 20 条核对准确率
3. 全量分析
4. 审核通过的建议 → 自动落成标签

## 安全须知

- **cookie115.txt**：你的 115 登录凭据，不要外泄
- **ai_config.json**：AI 接口的 API Key，不要外泄
- **115_tree.db**：包含你的完整网盘目录结构，属于隐私数据

## 常见问题

**Q：双击 bat 后闪退 / 提示找不到 Python？**
A：确认 Python 已安装并加入了 PATH。打开 cmd 输入 `python --version`。

**Q：Cookie 显示「失效」？**
A：按配置步骤重新获取 Cookie。

**Q：扫描很慢？**
A：扫描是串行的（3-5秒/目录），为了不触发 115 的风控。支持断点续扫。

**Q：删除的文件能恢复吗？**
A：删除操作会移入 115 回收站，30 天内可手动恢复。

**Q：想换台电脑用？**
A：把整个文件夹拷过去，新电脑装好 Python，双击 bat 即可。

## 开发说明

### 项目架构

- **server.py**：FastAPI 应用，提供 REST API
- **api115.py**：封装 115 网盘 API，使用 curl_cffi 绕过 WAF
- **scanner.py**：目录扫描管理器，支持定时扫描
- **transfer_worker.py**：转存工作线程，处理分享链接
- **ai_worker.py**：AI 分类工作线程，调用外部 AI API
- **tags.py**：标签系统，管理标签和关联
- **tag_rules.py**：规则引擎，按名字正则自动打标
- **dedup.py**：查重功能，分析重复资源
- **logbus.py**：日志系统，实时推送日志

### 扩展开发

1. **添加新功能**：在 server.py 中添加新的 API 端点
2. **修改前端**：编辑 static/ 目录下的 HTML/JS/CSS
3. **添加 AI 提供商**：修改 ai_worker.py 中的 API 调用逻辑
4. **自定义标签规则**：修改 tag_rules.py 中的正则表达式

## 许可证

本项目仅供个人学习使用，请勿用于商业用途。

## 更新日志

### v1.0.0
- 初始版本发布
- 支持目录浏览、扫描、转存、查重
- 支持 AI 自动分类
- 支持标签管理
- 支持备份恢复
