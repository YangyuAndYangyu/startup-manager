@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 正在以管理员身份启动（会弹 UAC 提示）……
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$exe = (Get-Command pythonw -ErrorAction SilentlyContinue).Source; if (-not $exe) { $exe = (Get-Command python).Source }; Start-Process -Verb RunAs -FilePath $exe -ArgumentList '\"%~dp0startup_manager.py\"'"
if errorlevel 1 pause
