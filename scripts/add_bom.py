"""为 PowerShell 脚本加上 UTF-8 BOM。

为什么需要：Windows PowerShell 5.1 默认按 ANSI/GBK 读取 .ps1，
没有 BOM 时脚本里的中文会破坏语法解析（报 "Missing closing '}'"）。
加 BOM 后 5.1 会按 UTF-8 正确解码。

用法：
    python scripts/add_bom.py                 # 处理 run.ps1 与 scripts/*.ps1
    python scripts/add_bom.py 路径1 路径2      # 只处理指定文件
"""

from __future__ import annotations

import sys
from pathlib import Path

BOM = b"\xef\xbb\xbf"
ROOT = Path(__file__).resolve().parents[1]


def targets(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(item) for item in argv]
    files = [ROOT / "run.ps1"] + sorted((ROOT / "scripts").glob("*.ps1"))
    return [path for path in files if path.exists()]


def ensure_bom(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(BOM):
        return "已有 BOM，跳过"
    text = data.decode("utf-8")
    if any(ord(ch) > 127 for ch in text):
        path.write_bytes(BOM + data)
        return "已加 UTF-8 BOM"
    path.write_bytes(data)
    return "纯 ASCII，无需 BOM"


def main(argv: list[str]) -> int:
    for path in targets(argv):
        status = ensure_bom(path)
        print(f"  {path.relative_to(ROOT)} -> {status}")
    print("\n完成。验证方法：用 powershell.exe 直接运行脚本，中文应正常显示。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
