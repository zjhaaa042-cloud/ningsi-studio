"""静态资源服务：只从配置的 web 目录读取，做路径穿越与越权 MIME 防护。"""

from __future__ import annotations

import mimetypes
from pathlib import Path

MIME_OVERRIDES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json; charset=utf-8",
}

INDEX_FILES = ("index.html",)


class StaticHandler:
    def __init__(self, web_dir) -> None:
        self.web_dir = Path(web_dir).resolve()

    def available(self) -> bool:
        return (self.web_dir / "index.html").exists()

    def resolve(self, path: str) -> Path | None:
        """把 URL 路径映射到磁盘文件；不合法或不存在返回 None。

        防护做两层：先显式拒绝 `..` 段与绝对路径（不依赖调用方是否已 unquote），
        再用 `relative_to` 兜底确认结果确实落在 web 目录内。
        """
        raw = (path or "").replace("\\", "/").lstrip("/")
        segments = [part for part in raw.split("/") if part]
        if any(part == ".." for part in segments) or raw.startswith("~"):
            return None
        relative = "/".join(segments) or "index.html"
        candidate = (self.web_dir / relative).resolve()
        try:
            candidate.relative_to(self.web_dir)
        except ValueError:
            return None
        if candidate.is_dir():
            for name in INDEX_FILES:
                nested = (candidate / name).resolve()
                try:
                    nested.relative_to(self.web_dir)
                except ValueError:
                    continue
                if nested.exists():
                    return nested
            return None
        return candidate if candidate.is_file() else None

    def content_type(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in MIME_OVERRIDES:
            return MIME_OVERRIDES[suffix]
        guessed, _ = mimetypes.guess_type(path.name)
        return guessed or "application/octet-stream"

    def load(self, path: Path) -> bytes:
        return path.read_bytes()
