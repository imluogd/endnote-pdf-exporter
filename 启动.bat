@echo off
rem 双击运行 endnote-pdf-exporter（EndNote 文献 PDF 导出，不显示命令行窗口）
setlocal
set "HERE=%~dp0"

REM 1) 优先用打包好的 exe
if exist "%HERE%dist\endnote-pdf-exporter.exe" (
    start "" "%HERE%dist\endnote-pdf-exporter.exe" %*
    exit /b 0
)

REM 2) 否则用本机 Python 运行源码
set "PYW="
for %%P in (pythonw.exe) do if not "%%~$PATH:P"=="" set "PYW=%%~$PATH:P"
if defined PYW goto run
where py >nul 2>&1 && for /f "usebackq delims=" %%I in (`py -3 -c "import os,sys;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"`) do set "PYW=%%I"
if defined PYW if exist "%PYW%" goto run

echo 没有找到 Python（pythonw.exe）。
echo 你可以用命令行运行：python "%~dp0app.py"
echo 或者按 packaging\build_exe.md 自己打包成 exe。
pause
exit /b 1

:run
start "" "%PYW%" "%HERE%app.py" %*
exit /b 0
