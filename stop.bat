@echo off
setlocal

rem ============================================================
rem  tableseed 停止脚本 —— 按端口找到服务进程并结束
rem  用法: stop.bat [端口]   默认 8643
rem ============================================================

set "PORT=8643"
if not "%~1"=="" set "PORT=%~1"

set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
    echo [tableseed] 结束进程 %%a (端口 %PORT%^)
    taskkill /F /PID %%a >nul 2>&1
    set FOUND=1
)

if "%FOUND%"=="0" echo [tableseed] 端口 %PORT% 上没有正在运行的服务。
