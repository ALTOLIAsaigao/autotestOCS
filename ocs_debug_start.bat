@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem ============================================================
rem  以调试端口启动 OCS Desktop
rem
rem  两个关键点：
rem    1) OCS 自身是【管理员权限】跑的 —— 普通权限 taskkill 会被
rem       "拒绝访问"。本脚本没提权时会自己请求提权。
rem    2) OCS 有单实例锁。旧实例没关掉，新实例会立刻自己退出
rem       （那个"闪退"是新实例，旧实例还活着）。
rem
rem  注意：本脚本第一次运行（没提权那个窗口）会在请求提权后停在
REM  pause 上，不会消失 —— 这样你能看到它说了什么。真正干活的是
rem  提权后新开的那个窗口，那个窗口结束时会 pause，不会闪掉。
rem ============================================================

rem  OCS 的安装位置。三种给法，从前往后试：
rem    1) 已经设过的环境变量 OCS_EXE
rem    2) 本目录下的 ocs_path.txt（里面就一行完整路径，斜杠引号都不用加）
rem    3) 下面这几个常见位置
if not defined OCS_EXE if exist "%~dp0ocs_path.txt" set /p OCS_EXE=<"%~dp0ocs_path.txt"
if not defined OCS_EXE for %%D in (
    "%LOCALAPPDATA%\Programs\OCS Desktop\OCS Desktop.exe"
    "%ProgramFiles%\OCS Desktop\OCS Desktop.exe"
    "%USERPROFILE%\Desktop\OCS Desktop\OCS Desktop.exe"
    "D:\OCS Desktop\OCS Desktop.exe"
    "E:\OCS Desktop\OCS Desktop.exe"
) do if not defined OCS_EXE if exist %%D set "OCS_EXE=%%~D"

set "PORT=9222"
rem OCS 拉起来的那个浏览器额外听的调试端口（worker/index.js 里的补丁认这个环境变量）
set "OCS_BROWSER_DEBUG_PORT=9223"

rem ---------- 0. 没提权就请求提权 ----------
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo [*] 当前不是管理员权限，正在请求提权...
    echo     会弹 UAC，点"是"。随后会新开一个管理员窗口干活，本窗口可以关掉。
    echo.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    if errorlevel 1 (
        echo [x] 提权失败。请右键本文件 -^> "以管理员身份运行"。
    )
    pause
    exit /b
)

echo ============================================================
echo  管理员窗口 —— 真正干活的是这里
echo ============================================================
echo.

if not exist "%OCS_EXE%" (
    echo [x] 找不到 OCS: "%OCS_EXE%"
    echo     把 OCS Desktop.exe 的完整路径写进 ocs_path.txt（一行，不加引号），
    echo     或者设个环境变量 OCS_EXE 再跑本脚本。
    goto :end
)

rem ---------- 1. 关掉旧 OCS，并确认真的关掉了 ----------
echo [*] 关闭正在运行的 OCS ...
taskkill /F /IM "OCS Desktop.exe" /T >nul 2>&1

set /a TRY=0
:waitkill
set /a TRY+=1
tasklist /FI "IMAGENAME eq OCS Desktop.exe" 2>nul | find /i "OCS Desktop.exe" >nul
if errorlevel 1 goto killed
if %TRY% GEQ 15 (
    echo [x] 15 秒了 OCS 还活着 —— 权限没上去。
    goto :end
)
timeout /t 1 /nobreak >nul
goto waitkill

:killed
echo [+] 旧实例已关闭。

rem ---------- 2. 带调试端口启动 ----------
rem  %OCS_BROWSER_DEBUG_PORT% 会被 OCS 继承，再传给它 fork 的 script.js，
rem  最终落到它拉起的 Chrome 的启动参数里（--remote-debugging-port=9223）。
echo [*] 执行: "%OCS_EXE%" --remote-debugging-port=%PORT%   [OCS_BROWSER_PORT=%OCS_BROWSER_DEBUG_PORT%]
start "" "%OCS_EXE%" --remote-debugging-port=%PORT%

timeout /t 3 /nobreak >nul
tasklist /FI "IMAGENAME eq OCS Desktop.exe" 2>nul | find /i "OCS Desktop.exe" >nul
if errorlevel 1 (
    echo [x] 新起的 OCS 立刻退出了 —— 还有别的 OCS 实例活着（单实例锁）。
    echo     任务管理器里把 OCS Desktop.exe 全结束掉，再重跑本脚本。
    goto :end
)

rem ---------- 3. 等调试端口就绪 ----------
echo [*] 等待调试端口 %PORT% ...
set /a N=0
:waitport
set /a N+=1
powershell -NoProfile -Command "try{ (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:%PORT%/json/version').StatusCode }catch{ exit 1 }" >nul 2>&1
if not errorlevel 1 goto ready
if %N% GEQ 30 (
    echo [x] 等了一分多钟，调试端口还是没起来。
    echo     OCS 进程在跑，但 %PORT% 没监听。手工确认一下:
    echo         netstat -ano ^| findstr :%PORT%
    goto :end
)
timeout /t 2 /nobreak >nul
goto waitport

:ready
echo.
echo [+] 成功: http://127.0.0.1:%PORT%/json
echo [+] 下一步:  python ocs_click_play.py --list
echo.

:end
echo ============================================================
echo  脚本结束。这个窗口不会自动关闭。
echo ============================================================
pause
endlocal
