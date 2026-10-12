@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   正在打包「开机启动管理器」为单文件 exe …
echo ============================================

echo [1/3] 安装打包工具 PyInstaller ……
python -m pip install --upgrade pyinstaller
if errorlevel 1 ( echo 安装失败，请检查网络。 & pause & exit /b 1 )

echo [2/3] 生成程序图标 ……
python make_icon.py

echo [3/3] 打包中，大约需要 1~3 分钟 ……
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name 开机启动管理器 ^
  --icon egg.ico ^
  --version-file version_info.txt ^
  startup_manager.py

if errorlevel 1 ( echo 打包失败。 & pause & exit /b 1 )

echo.
echo ============================================
echo   完成！双击运行：
echo   %~dp0dist\开机启动管理器.exe
echo ============================================
pause