"""静态自检：不启动服务也能发现的低级问题。

检查项：
1. 所有 Python 文件可被 `ast` 解析（语法/缩进）；
2. 事件总线里发布过的事件类型，是否都出现在文档契约中；
3. 路由表里注册的接口路径，是否都出现在 `docs/API.md` 中（避免前后端口径漂移）；
4. 前端 `web/` 是否齐全，且 `index.html` 引用的本地资源都存在；
5. 前端 JS 里出现的 `/api/...` 字面量路径，是否都能在路由表里找到对应的模式。

用法：
    python -m ningsi_studio.tools.static_check        （需要 PYTHONPATH=src）
或：
    .\\run.ps1 -Action check
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]          # ningsi-studio/  (tools/ -> ningsi_studio/ -> src/ -> ROOT)
SRC = ROOT / "src"
PACKAGE = SRC / "ningsi_studio"
WEB = ROOT / "web"
DOCS = ROOT / "docs"

API_PATH_RE = re.compile(r"/api/[A-Za-z0-9_\-{}/.]*(?![A-Za-z0-9_\-{}/.|])")
JS_STRING_RE = re.compile(r"""["'`](/api/[^"'`\s]*)["'`]""")

PLACEHOLDER = re.compile(r"\{[a-z_]+\}")


def iter_python_files() -> list[Path]:
    files = sorted(SRC.rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py"))
    return [path for path in files if "__pycache__" not in path.parts]


def check_syntax() -> list[str]:
    problems = []
    for path in iter_python_files():
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            problems.append(f"语法错误 {path.relative_to(ROOT)}:{exc.lineno} {exc.msg}")
    return problems


def route_patterns() -> list[str]:
    text = (PACKAGE / "api" / "routes.py").read_text(encoding="utf-8")
    return re.findall(r'@router\.(?:get|post|patch|delete)\(r"([^"]+)"\)', text)


def doc_paths() -> set[str]:
    text = (DOCS / "API.md").read_text(encoding="utf-8")
    return set(API_PATH_RE.findall(text))


def published_events() -> set[str]:
    events: set[str] = set()
    for path in sorted((SRC / "ningsi_studio").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        events.update(re.findall(r'publish\(\s*"([a-z_]+)"', text))
    # EventBus 内部使用的控制事件
    events.update({"closed"})
    return events


def documented_events() -> set[str]:
    text = (DOCS / "API.md").read_text(encoding="utf-8")
    start = text.find("| event | data 关键字段 |")
    if start < 0:
        return set()
    block = text[start:text.find("\n\n", start)]
    found = set()
    for line in block.splitlines()[2:]:
        cell = line.split("|")[1].strip() if line.count("|") >= 2 else ""
        for name in re.findall(r"`?([a-z_]{3,})`?", cell):
            found.add(name)
    return found


def check_routes(patterns: list[str]) -> list[str]:
    documented = doc_paths()
    missing = []
    for pattern in patterns:
        normalized = normalize_path(pattern)
        if not any(normalize_path(path) == normalized for path in documented):
            missing.append(pattern)
    return [f"接口未写入 docs/API.md: {item}" for item in missing]


def normalize_path(path: str) -> str:
    """把路径参数统一成 {x}：文档里写 {SAS|SDS}、{sart|pvt} 也算同一个端点。"""
    text = path.rstrip("/")
    text = re.sub(r"\{[^/}]*\}", "{x}", text)
    return text


def check_events() -> list[str]:
    published = {name for name in published_events() if name not in {"closed", "message"}}
    documented = documented_events()
    missing = sorted(name for name in published if name not in documented)
    return [f"事件未写入 docs/API.md: {item}" for item in missing]


def check_frontend(patterns: list[str]) -> list[str]:
    problems: list[str] = []
    if not WEB.exists():
        return ["前端目录缺失：web/"]
    index = WEB / "index.html"
    if not index.exists():
        return ["缺少 web/index.html"]
    html = index.read_text(encoding="utf-8")
    for reference in re.findall(r'(?:src|href)="([^"]+)"', html):
        if reference.startswith(("http://", "https://", "//", "data:", "#")):
            continue
        target = (WEB / reference.lstrip("./")).resolve()
        if not target.exists():
            problems.append(f"index.html 引用了不存在的资源: {reference}")
    for path in sorted(WEB.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        for literal in set(JS_STRING_RE.findall(text)):
            if "{" in literal or literal.endswith("/"):
                continue
            if not any(literal_matches(literal, pattern) for pattern in patterns):
                problems.append(f"{path.relative_to(ROOT)} 调用了未注册的接口: {literal}")
    return problems


def literal_matches(literal: str, pattern: str) -> bool:
    """把路由模式转成正则，判断一串实际路径是否会被它匹配。"""
    regex = re.escape(pattern).replace(r"\{", "{").replace(r"\}", "}")
    regex = re.sub(r"\{[a-z_]+\}", "[^/]+", regex)
    return re.fullmatch(regex, literal) is not None


def check_docs() -> list[str]:
    problems = []
    for name in ("API.md",):
        if not (DOCS / name).exists():
            problems.append(f"缺少 docs/{name}")
    for name in ("README.md", "STUDIO-GUIDE.md", "pyproject.toml"):
        if not (ROOT / name).exists():
            problems.append(f"缺少 {name}")
    return problems


def main() -> int:
    problems: list[str] = []
    patterns = route_patterns()

    print(f"扫描目录：{ROOT}")
    problems += check_syntax()
    print(f"  Python 文件     : {len(iter_python_files())} 个（语法检查）")
    print(f"  路由            : {len(patterns)} 条")
    print(f"  文档事件        : {len(documented_events())} 种")
    print(f"  发布事件        : {len(published_events())} 种")
    problems += check_routes(patterns)
    problems += check_events()
    problems += check_frontend(patterns)
    problems += check_docs()

    if problems:
        print(f"\n发现 {len(problems)} 个问题：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\n静态自检通过：语法、接口文档、事件文档、前端资源与接口调用一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
