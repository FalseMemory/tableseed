@echo off
setlocal
cd /d "%~dp0"

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
if "%~1"=="" goto ready
shift
goto parse

:ready
if not exist ".venv\Scripts\python.exe" (
    echo [tableseed] 首次运行: 创建虚拟环境并安装依赖...
    python -m venv .venv
    if errorlevel 1 goto fail
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt -r requirements-dev.txt
    if errorlevel 1 goto fail
)

if not exist "%CONFIG%" (
    echo [tableseed] 配置文件不存在: %CONFIG%
    goto fail
)

echo [tableseed] WebUI: http://127.0.0.1:%PORT%  (Ctrl+C 停止)
".venv\Scripts\tableseed.exe" ui -c "%CONFIG%" --port %PORT%
goto :eof

:fail
echo [tableseed] 启动失败, 请检查 Python 环境与配置文件。
exit /b 1
