#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
115 网盘管理器启动脚本
自动检查环境、安装依赖、启动服务
"""

import os
import sys
import subprocess
import platform
import shutil

def check_python_version():
    """检查 Python 版本"""
    if sys.version_info < (3, 9):
        print("错误: 需要 Python 3.9 或更高版本")
        print(f"当前版本: {sys.version}")
        print("下载地址: https://www.python.org/downloads/")
        return False
    return True

def check_dependencies():
    """检查依赖是否安装"""
    try:
        import fastapi
        import uvicorn
        import curl_cffi
        import apscheduler
        import openpyxl
        import multipart
        import pydantic
        import aiofiles
        return True
    except ImportError as e:
        print(f"缺少依赖: {e}")
        return False

def install_dependencies():
    """安装依赖"""
    print("正在安装依赖...")
    try:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "-r", "requirements.txt"
        ])
        print("依赖安装成功")
        return True
    except subprocess.CalledProcessError:
        print("依赖安装失败")
        print("请手动执行: pip install -r requirements.txt")
        return False

def check_cookie():
    """检查 Cookie 配置"""
    cookie_file = "cookie115.txt"
    if not os.path.exists(cookie_file):
        print(f"警告: 未找到 {cookie_file}")
        print("请先配置 Cookie，否则无法连接 115 网盘")
        print("配置方法:")
        print("1. 用 Chrome/Edge 浏览器打开 https://115.com 并登录")
        print("2. 按 F12 打开开发者工具")
        print("3. 切换到 Network 标签")
        print("4. 点击页面触发请求")
        print("5. 找到 Request Headers → Cookie")
        print("6. 复制整行 Cookie 值")
        print(f"7. 创建 {cookie_file} 文件，粘贴 Cookie 内容")
        return False
    
    with open(cookie_file, "r", encoding="utf-8") as f:
        content = f.read().strip()
    
    if not content or ("UID" not in content and "uid" not in content):
        print(f"警告: {cookie_file} 内容无效")
        print("Cookie 应包含 UID 字段")
        return False
    
    print("Cookie 配置正常")
    return True

def get_local_ip():
    """获取本机局域网 IP"""
    try:
        if platform.system() == "Windows":
            import socket
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            return local_ip
        else:
            import subprocess
            result = subprocess.run(
                ["ip", "route", "get", "1"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                lines = result.stdout.split("\n")
                for line in lines:
                    if "via" in line:
                        parts = line.split()
                        for i, part in enumerate(parts):
                            if part == "via" and i + 1 < len(parts):
                                return parts[i + 1]
    except Exception:
        pass
    return "未知"

def start_server():
    """启动服务器"""
    print("\n" + "=" * 50)
    print("           115 网盘管理器")
    print("=" * 50)
    print()
    print("启动中...")
    print()
    print(f"访问地址: http://127.0.0.1:8765")
    print(f"局域网访问: http://{get_local_ip()}:8765")
    print()
    print("按 Ctrl+C 停止服务")
    print("=" * 50)
    print()
    
    # 启动浏览器（后台）
    try:
        import webbrowser
        import threading
        import time
        
        def open_browser():
            time.sleep(3)
            webbrowser.open("http://127.0.0.1:8765")
        
        browser_thread = threading.Thread(target=open_browser, daemon=True)
        browser_thread.start()
    except Exception:
        pass
    
    # 启动服务
    try:
        import uvicorn
        uvicorn.run(
            "server:app",
            host="0.0.0.0",
            port=8765,
            reload=False,
            log_level="info"
        )
    except KeyboardInterrupt:
        print("\n服务已停止")
    except Exception as e:
        print(f"\n启动失败: {e}")
        print("请检查端口是否被占用")

def main():
    """主函数"""
    print("115 网盘管理器启动检查...")
    print()
    
    # 检查 Python 版本
    if not check_python_version():
        input("按回车键退出...")
        return
    
    print(f"Python 版本: {sys.version}")
    
    # 检查依赖
    if not check_dependencies():
        print("首次运行，正在安装依赖...")
        if not install_dependencies():
            input("按回车键退出...")
            return
    
    print("依赖检查通过")
    
    # 检查 Cookie
    check_cookie()
    
    # 启动服务
    start_server()

if __name__ == "__main__":
    main()
