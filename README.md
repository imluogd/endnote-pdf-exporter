# endnote-pdf-exporter

从 EndNote 文献库导出选中文献的 PDF，供导入知识库程序。

在 EndNote 文献库里点选一篇或多篇文献，把它们附带的 PDF 复制到一个文件夹，再导出。

工具只读取 EndNote 库，不会修改、移动或删除你的文献库。

---

## 快速开始

1. 下载/运行程序：从 [Releases](../../releases) 下载 **`endnote-pdf-exporter.exe`** 双击运行；或双击 `启动.bat`；或命令行 `python app.py`。
2. 点 **「＋ 添加文献库」**，选择你的 EndNote 库文件（`.enl`，也支持压缩库 `.enlx`）。
   第一次选好后程序会记住，下次自动打开。
3. 左边点一个 **分组**（或「全部文献」）；右边列表里按住 **Ctrl / Shift 点选**文献，
   或者按 **Ctrl+A 全选**。
   - 只想要有 PDF 的？灰色行就是「只有文献记录、没有 PDF」的条目，导出时会跳过并提示。
4. 点 **「导出选中 PDF…」**，选一个文件夹（比如 `D:\test`），点 **开始导出**。

导入完成后就可以删掉这个导出文件夹，EndNote 里的库不受影响。

---

## 文件名规则

默认：**`年份-作者-标题.pdf`**，例如

```
1994-Broadbelt-Computer Generated Pyrolysis Modeling On-the-Fly Generation of Species, Reactions, and Rates.pdf
```

## 已知限制

- 只支持 EndNote 桌面版（Windows，X8 及以后的新格式库 `.enl/.enlx`）。
- 只导出 PDF，不导出其他附件类型。
- 不读 EndNote 的"最近添加"窗口内容（用「全部文献」按年份排序即可）。
