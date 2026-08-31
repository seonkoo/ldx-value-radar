@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  李大霄价值雷达 - 互联网服务（内网穿透，不依赖 GitHub）
REM  双击运行：本机起服务 + 拉起穿透 + 弹出公网地址二维码
REM  逻辑全部在 start_net.py 内，这里只做环境探测与调用
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

REM ---- 执行（服务会常驻，Ctrl+C 退出后窗口等待按键） ----
"%PY%" "%ROOT%\scripts\start_net.py" %*
echo.
echo [已退出] 按任意键关闭窗口
pause >nul
exit /b 0
