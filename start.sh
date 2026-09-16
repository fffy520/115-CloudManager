#!/bin/bash

# 115 网盘管理器启动脚本 (Linux/macOS)

echo "=========================================="
echo "           115 网盘管理器"
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

# 检查依赖
echo "检查依赖..."
if ! $PY -c "import fastapi" &> /dev/null; then
    echo "首次运行，正在安装依赖（约1分钟）..."
    $PY -m pip install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo "[错误] 依赖安装失败，请手动执行: $PY -m pip install -r requirements.txt"
        exit 1
    fi
    echo "依赖安装成功"
else
    echo "依赖已安装"
fi

echo
echo "=========================================="
echo "  115 网盘管理器 启动中..."
echo "  浏览器将自动打开 http://127.0.0.1:8765"
echo "  局域网设备访问: http://本机IP:8765"
echo "  按 Ctrl+C 停止服务"
echo "=========================================="
echo

# 获取本机 IP
if command -v ip &> /dev/null; then
    LOCAL_IP=$(ip route get 1 | awk '{print $7; exit}')
elif command -v ifconfig &> /dev/null; then
    LOCAL_IP=$(ifconfig | grep -Eo 'inet (addr:)?([0-9]*\.){3}[0-9]*' | grep -Eo '([0-9]*\.){3}[0-9]*' | grep -v '127.0.0.1' | head -1)
else
    LOCAL_IP="未知"
fi

echo "本机局域网 IP: $LOCAL_IP"
echo

# 启动浏览器（后台）
if command -v xdg-open &> /dev/null; then
    (sleep 3 && xdg-open http://127.0.0.1:8765) &
elif command -v open &> /dev/null; then
    (sleep 3 && open http://127.0.0.1:8765) &
fi

# 启动服务
$PY server.py
