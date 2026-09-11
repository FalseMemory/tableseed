@echo off
setlocal
cd /d "%~dp0"
title tableseed

rem ============================================================
rem  tableseed 启动脚本
rem  用法:
rem    start.bat                          默认端口 8643, 配置 samples\txn.yaml
rem    start.bat -p 9000                  指定端口
rem    start.bat -c samples\account.yaml  指定配置
rem ============================================================

set "PORT=8643"
set "CONFIG=samples\txn.yaml"

:parse
if "%~1"=="-p"        (set "PORT=%~2"     & shift & shift & goto parse)
if "%~1"=="--port"    (set "PORT=%~2"     & shift & shift & goto parse)
if "%~1"=="-c"        (set "CONFIG=%~2"   & shift & shift & goto parse)
if "%~1"=="--config"  (set "CONFIG=%~2"   & shift & shift & goto parse)
if "%~1"=="" goto findpy
shift
goto parse

:findpy
rem ---- 定位可用的 Python（Windows 商店的 python 假别名会被真实调用淘汰）----
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY where py >nul 2>&1 && set "PY=py -3"

if not defined PY goto no_python

rem ---- 真正调用一次验证可用性（商店假别名会在这里失败）----
%PY% -c "print(1)" >nul 2>&1
if errorlevel 1 (
    set "PY="
    if not defined PY where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY goto no_python

if not exist "%CONFIG%" (
    echo [tableseed] 配置文件不存在: %CONFIG%
    goto fail
)

rem ---- 首次运行：创建虚拟环境并安装依赖 ----
if not exist ".venv\Scripts\python.exe" (
    echo [tableseed] 首次运行: 创建虚拟环境并安装依赖, 可能需要几分钟...
    %PY% -m venv .venv
    if errorlevel 1 goto fail_venv
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt -r requirements-dev.txt
    if errorlevel 1 goto fail_pip
)

echo [tableseed] WebUI: http://127.0.0.1:%PORT%  (按 Ctrl+C 停止服务)
".venv\Scripts\tableseed.exe" ui -c "%CONFIG%" --port %PORT%

echo.
echo [tableseed] 服务已停止。
pause
goto :eof

:no_python
echo.
echo  [tableseed] 未找到可用的 Python, 无法启动。
echo.
echo  请先安装 Python 3.10 以上版本:
echo     https://www.python.org/downloads/
echo  安装时务必勾选 "Add Python to PATH", 装完重新打开本脚本。
echo.
echo  如果你用的是 Microsoft Store 版 Python, 建议改用官网安装包。
echo.
pause
exit /b 1

:fail_venv
echo [tableseed] 创建虚拟环境失败。常见原因:
echo   1. python 是 Microsoft Store 的假别名 —— 请从 python.org 安装并勾选 Add to PATH
echo   2. 目录权限不足 —— 请勿放在受控的系统目录下
goto fail

:fail_pip
echo [tableseed] 依赖安装失败, 常见原因是网络不通。
echo   公司内网可尝试配置代理或内网 PyPI 源后重新运行本脚本。
goto fail

:fail
echo.
pause
exit /b 1