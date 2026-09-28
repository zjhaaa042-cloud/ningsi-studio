"""产物打包：把一次会话的产出（报告/热力图/趋势/模型/台账）打成 zip，带 manifest 与校验值。"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from ningsi_studio.db.sqlite_store import utcnow


def sha256_of(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_manifest(path) -> dict:
    target = Path(path)
    if not target.exists():
        return {"path": str(target), "exists": False}
    return {"path": str(target), "exists": True, "bytes": target.stat().st_size,
            "sha256": sha256_of(target)}


def build_zip(zip_path, files: dict, meta: dict) -> Path:
    """files: {kind: Path}；meta: 会话元数据。返回 zip 路径。"""
    target = Path(zip_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generated_at": utcnow(),
        "meta": meta,
        "files": {kind: file_manifest(path) for kind, path in files.items()},
    }
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for kind, path in files.items():
            candidate = Path(path)
            if candidate.exists() and candidate.is_file():
                archive.write(candidate, arcname=f"{kind}/{candidate.name}")
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return target
