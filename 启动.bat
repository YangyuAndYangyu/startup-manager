@echo off
chcp 65001 >nul
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "startup_manager.py"
    exit /b
)

where python >nul 2>nul
if %errorlevel%==0 (
    echo 没有找到 pythonw，改用 python 启动（会多一个控制台窗口）……
    python "startup_manager.py"
    exit /b
)

echo 没有找到 Python。请先安装 Python 3（安装时勾选 Add Python to PATH）。
pause
