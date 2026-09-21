@echo off
rem 互传 - Windows 启动（零依赖，双击或命令行运行）
rem   启动后自动打开本机控制台；手机/其他电脑用窗口里打印的局域网地址访问。
rem   关闭本窗口即停止服务。
rem   服务本体在 ..\src\server.py（与 .app bundle 内是同一份源码）。
setlocal
cd /d "%~dp0"

if not exist "%~dp0..\src\server.py" (
  echo 错误：找不到 %~dp0..\src\server.py
  echo 请保持 scripts\ 与 src\ 同级的目录结构再运行。
  pause
  exit /b 1
)

rem 找 Python 3：py 启动器 -> python。都没有就明说。
set PY=
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>&1
if not errorlevel 1 set PY=py -3
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>&1
  if not errorlevel 1 set PY=python
)
if not defined PY (
  echo 错误：没找到 Python 3（需 3.8+）。请先安装：https://www.python.org/downloads/
  echo 安装时勾选 "Add Python to PATH"。
  pause
  exit /b 1
)

if not defined PORT set PORT=8765
if not defined PORT_SPAN set PORT_SPAN=20
if not defined INBOX set INBOX=%USERPROFILE%\Downloads\android-inbox
if not defined OUTBOX set OUTBOX=%USERPROFILE%\Downloads\android-outbox
set OPEN_CONSOLE=1

%PY% "%~dp0..\src\server.py"
pause
