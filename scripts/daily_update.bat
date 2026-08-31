@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  李大霄价值雷达 - 本地每日更新
REM  由 Windows 任务计划每日触发；也可双击手动运行
REM  逻辑全部在 update_and_push.py 内，这里只做环境探测与调用
REM ============================================================

setlocal

REM 定位到项目根目录（本脚本位于 scripts\ 下）
pushd "%~dp0.."
set "ROOT=%CD%"

REM ---- 探测 Python ----
where python >nul 2>&1
if errorlevel 1 (
    where py >nul 2>&1
    if errorlevel 1 (
        echo [FATAL] 未找到 python / py，请确认已安装并加入 PATH
        echo 若已安装但未加入 PATH，请修改本脚本，把 python 换成完整路径
        echo 例如：set "PY=C:\Users\seon\AppData\Local\Programs\Python\Python311\python.exe"
        pause
        exit /b 1
    )
    set "PY=py"
) else (
    set "PY=python"
)

REM ---- 执行 ----
"%PY%" "%ROOT%\scripts\update_and_push.py"
set "RC=%ERRORLEVEL%"

popd

if not "%RC%"=="0" (
    echo.
    echo [失败] 退出码 %RC% ，详见 %ROOT%\logs\daily_*.log
    exit /b %RC%
)
exit /b 0
