# 重新打包 exe（Windows，PowerShell）

本工具用 PyInstaller 打成单文件 `dist\endnote-pdf-exporter.exe`。
需要一个带 tkinter 的 Python 环境（开发时用的是 conda 环境），下面用 `<env>` 表示其根目录。

```powershell
$env2 = '<env>'          # 例如 C:\Users\<你>\miniconda3\envs\py312
$py = "$env2\python.exe"

& $py -m pip install --user pyinstaller   # 只需一次

& $py -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name 'endnote-pdf-exporter' `
  --distpath '<项目目录>\dist' --workpath '<项目目录>\build' --specpath '<项目目录>\build' `
  --add-binary "$env2\Library\bin\tcl86t.dll;." `
  --add-binary "$env2\Library\bin\tk86t.dll;." `
  --add-binary "$env2\Library\bin\sqlite3.dll;." `
  --add-data "$env2\Library\lib\tcl8.6;tcl8.6" `
  --add-data "$env2\Library\lib\tk8.6;tk8.6" `
  --runtime-hook '<项目目录>\packaging\rthook_tcl.py' `
  '<项目目录>\app.py'
```

## 为什么要手动带 tcl/tk/sqlite3

conda 版 Python 把 `tcl86t.dll`、`tk86t.dll`、`sqlite3.dll` 放在
`<env>\Library\bin` 而不是 Python 根目录，PyInstaller 默认不会收集，打出来的 exe 会报
`DLL load failed while importing _tkinter / _sqlite3`。上面的 `--add-binary` 把它们放进
exe，`rthook_tcl.py` 在启动时把 `TCL_LIBRARY` / `TK_LIBRARY` 指向内置的 tcl/tk 数据目录。

> 用 python.org 的官方 Python 打包时通常不需要这些额外参数（PyInstaller 自带的 tkinter
> hook 能找齐 DLL），可以先试不加 `--add-binary/--add-data` 的命令。

## 打包后自检（不打开界面）

```powershell
dist\endnote-pdf-exporter.exe --selftest `
  --library "<你的库>.enl" `
  --group "某个分组名" --out "$env:TEMP\exe_selftest" `
  --report "$env:TEMP\exe_selftest.json"
```

报告里 `"frozen": true` 表示确实跑在打包后的 exe 里；`exported` 为导出的 PDF 数。
不加 `--group` 时默认取前 10 篇有 PDF 的文献。
