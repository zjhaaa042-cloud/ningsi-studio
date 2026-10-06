"""PyInstaller 打包入口：把 `python -m ningsi_studio <子命令>` 变成 exe 的参数。

之所以要单独一个入口文件：PyInstaller 只能以「脚本」为入口，不能直接 `-m 包`。
这里只做转发，参数解析仍在 `ningsi_studio.__main__:main`，保证 exe 与源码运行**同一套 CLI**。
"""

from __future__ import annotations

import multiprocessing
import sys

from ningsi_studio.__main__ import main

if __name__ == "__main__":
    # 冻结后如果以后用到 multiprocessing，必须先 freeze_support，否则会反复启动自己
    multiprocessing.freeze_support()
    sys.exit(main())
