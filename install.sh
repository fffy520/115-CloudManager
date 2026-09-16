#!/bin/bash

# 115 网盘管理器安装脚本

echo "=========================================="
echo "           115 网盘管理器安装"
echo "=========================================="
echo

# 检查 Python
if ! command -v python3 &> /dev/null; then
    if ! command -v python &> /dev/null; then
        echo "[错误] 未找到 Python，请先安装 Python 3.9+"
        echo "下载地址: https://www.python.org/downloads/"
        exit 1
    else
        PY="python"
    fi
else
    PY="python3"
fi

echo "使用 Python: $($PY --version)"

# 检查 Python 版本
PYTHON_VERSION=$($PY -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]); then
    echo "[错误] 需要 Python 3.9 或更高版本"
    echo "当前版本: $PYTHON_VERSION"
    echo "下载地址: https://www.python.org/downloads/"
    exit 1
fi

echo "Python 版本检查通过"

# 检查 pip
if ! command -v pip3 &> /dev/null; then
    if ! command -v pip &> /dev/null; then
        echo "[错误] 未找到 pip，请先安装 pip"
        exit 1
    else
        PIP="pip"
    fi
else
    PIP="pip3"
fi

echo "使用 pip: $PIP"

# 安装依赖
echo
echo "正在安装依赖..."
$PIP install -r requirements.txt

if [ $? -ne 0 ]; then
    echo "[错误] 依赖安装失败"
    echo "请手动执行: $PIP install -r requirements.txt"
    exit 1
fi

echo "依赖安装成功"

# 检查 Cookie
echo
if [ ! -f "cookie115.txt" ]; then
    echo "[警告] 未找到 cookie115.txt"
    echo "请先配置 Cookie，否则无法连接 115 网盘"
    echo
    echo "配置方法:"
    echo "1. 用 Chrome/Edge 浏览器打开 https://115.com 并登录"
    echo "2. 按 F12 打开开发者工具"
    echo "3. 切换到 Network 标签"
    echo "4. 点击页面触发请求"
    echo "5. 找到 Request Headers → Cookie"
    echo "6. 复制整行 Cookie 值"
    echo "7. 创建 cookie115.txt 文件，粘贴 Cookie 内容"
    echo
else
    echo "Cookie 文件已存在"
fi

# 创建 .env 文件（如果不存在）
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "已创建 .env 配置文件"
    fi
fi

# 创建 ai_config.json 文件（如果不存在）
if [ ! -f "ai_config.json" ]; then
    if [ -f "ai_config.example.json" ]; then
        cp ai_config.example.json ai_config.json
        echo "已创建 ai_config.json 配置文件"
    fi
fi

echo
echo "=========================================="
echo "           安装完成！"
echo "=========================================="
echo
echo "启动方法:"
echo "  1. 运行 ./start.sh"
echo "  2. 或运行 python3 run.py"
echo "  3. 或运行 python3 server.py"
echo
echo "访问地址: http://127.0.0.1:8765"
echo
