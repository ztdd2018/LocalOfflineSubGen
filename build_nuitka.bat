@echo off
REM ============================================================
REM LocalOfflineSubGen Windows 绿色版打包脚本（Nuitka）
REM 前置条件：
REM   1. Python 3.10+，已执行 pip install -r requirements.txt
REM      以及 pip install nuitka ordered-set zstandard
REM   2. 已执行 scripts\fetch_ffmpeg.ps1，bin\ 下存在 ffmpeg.exe / ffprobe.exe
REM 产物：dist\LocalOfflineSubGen\ 绿色解压包（免安装，可直接拷贝运行）
REM ============================================================
setlocal
cd /d %~dp0

python -m nuitka ^
  --standalone ^
  --enable-plugin=pyqt6 ^
  --include-data-dir=bin=bin ^
  --include-package=faster_whisper ^
  --include-package=ctranslate2 ^
  --include-package=tokenizers ^
  --include-package=av ^
  --include-package=huggingface_hub ^
  --nofollow-import-to=pytest,unittest,tests ^
  --windows-console-mode=disable ^
  --company-name=LocalOffline ^
  --product-name=LocalOfflineSubGen ^
  --file-version=1.0.0.0 ^
  --output-dir=dist ^
  --remove-output ^
  --assume-yes-for-downloads ^
  main.py

if errorlevel 1 (
  echo [失败] Nuitka 打包失败，请检查上方输出
  exit /b 1
)

REM 整理绿色包目录名
if exist dist\main.dist ren dist\main.dist LocalOfflineSubGen
echo [完成] 绿色版已输出到 dist\LocalOfflineSubGen\ ，可直接整体拷贝分发
endlocal
