@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 调试模式：如果软件打不开，这里会显示错误信息。
python "startup_manager.py"
echo.
echo 程序已退出（错误码 %errorlevel%）
pause
