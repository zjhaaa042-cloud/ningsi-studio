"""凝思 Studio 后端自动化测试包。

运行方式（工作目录 `ningsi-studio`）：

    python -m unittest discover -s tests -t .

只使用标准库（unittest / urllib / http.client / sqlite3 / tempfile），
每个测试类自起一个真实 `http.server`，数据目录用临时目录，互不干扰。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _add_to_path(candidate: Path) -> None:
    """把存在的目录加入 sys.path（幂等）。"""
    text = str(candidate)
    if candidate.exists() and text not in sys.path:
        sys.path.insert(0, text)


# 后端源码（src 布局；未 pip install 时也能直接 import ningsi_studio）
_add_to_path(PROJECT_ROOT / "src")

# 上游算法引擎：与 ningsi_studio.bootstrap 的候选顺序保持一致
for _candidate in (
    PROJECT_ROOT.parent / "ningsi" / "src",
    PROJECT_ROOT.parent / "ningsi" / "ningsi" / "src",
    PROJECT_ROOT / "vendor" / "ningsi" / "src",
):
    _add_to_path(_candidate)
del _candidate
