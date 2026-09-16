@echo off
setlocal
title 115 网盘管理器
cd /d "%~dp0"

rem 清掉外部 PYTHONIOENCODING, 让 Python 用控制台原生编码输出, 保证中文不乱码
set "PYTHONIOENCODING="

echo ============================================
echo            115 网盘管理器
echo ============================================
echo.

rem ---------- 1. 重复双击检测：端口已监听则直接开浏览器 ----------
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo 服务已在运行，正在打开浏览器...
    start "" "http://127.0.0.1:8765"
    timeout /t 2 >nul
    exit /b 0
)

rem ---------- 2. 查找 Python ----------
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [错误] 未检测到 Python，请先安装 Python 3.9 或更高版本：
    echo        https://www.python.org/downloads/
    echo        安装时务必勾选 "Add Python to PATH"，装完后重新双击本文件。
    echo.
    pause
    exit /b 1
)

rem ---------- 3. 检查依赖，缺失则自动安装 ----------
"%PY%" -c "import fastapi,uvicorn,curl_cffi,apscheduler,openpyxl,multipart" >nul 2>nul
if errorlevel 1 (
    echo [1/2] 首次运行，正在安装依赖（约 1 分钟，请耐心等待）...
    echo.
    "%PY%" -m pip install -r "..\requirements.txt"
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络连接后重新双击本文件。
        pause
        exit /b 1
    )
    echo.
    echo 依赖安装完成。
) else (
    echo [1/2] 依赖已就绪
)

rem ---------- 4. 端口就绪后自动打开浏览器 ----------
echo [2/2] 正在启动服务...
echo.
echo    访问地址：http://127.0.0.1:8765
echo    局域网访问：http://本机IP:8765
echo    停止服务：直接关闭本窗口
echo.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 60;$i++){try{$c=New-Object Net.Sockets.TcpClient('127.0.0.1',8765);$c.Close();Start-Process 'http://127.0.0.1:8765';break}catch{Start-Sleep -Milliseconds 500}}"

"%PY%" server.py

echo.
echo 服务已停止。
pause
