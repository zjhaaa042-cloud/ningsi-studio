"""引擎定位：保证 `import ningsi` 可用，并给出可诊断的错误信息。

三种情况依次尝试：
1. `ningsi` 已在当前解释器中可导入（已 pip install -e 或位于 sys.path）；
2. 同仓库旁的 `../ningsi/src` 或 `../../ningsi/src`（工作区内并排存放的常见形态）；
3. 同仓库下的 `vendor/ningsi/src`（把引擎源码一起提交时的形态）。

全部失败时抛出 EngineNotFound，附带处理办法，不做静默降级。
"""

from __future__ import annotations

import sys
from pathlib import Path

ENGINE_HINT = (
    "未找到凝思引擎包 ningsi。请任选一种方式准备：\n"
    "  1) pip install -e ../ningsi          （工作区内并排的引擎源码）\n"
    "  2) pip install -e ../ningsi/ningsi   （引擎源码位于一层子目录时）\n"
    "  3) pip install ningsi                （已发布版本）\n"
    "  4) set PYTHONPATH=..\\ningsi\\src;..\\ningsi （不安装，直接指向 src）\n"
    "也可以用 scripts\\bootstrap.ps1 一键完成。"
)


class EngineNotFound(RuntimeError):
    """引擎包不可导入。"""


def _candidate_roots() -> list[Path]:
    here = Path(__file__).resolve()
    package_root = here.parents[1]          # src/
    project_root = here.parents[2]          # ningsi-studio/
    workspace = project_root.parent         # 工作区
    candidates = [
        workspace / "ningsi" / "src",
        workspace / "ningsi" / "ningsi" / "src",
        project_root / "vendor" / "ningsi" / "src",
        package_root.parent.parent / "ningsi" / "src",
    ]
    seen: list[Path] = []
    for item in candidates:
        if item not in seen:
            seen.append(item)
    return seen


def engine_src_dirs() -> list[Path]:
    """返回存在的候选引擎源码目录（已注入 sys.path 或可注入的）。"""
    return [root for root in _candidate_roots() if (root / "ningsi" / "__init__.py").exists()]


def ensure_engine() -> str:
    """确保引擎可导入，返回引擎包文件位置。"""
    try:
        import ningsi  # noqa: F401
        return str(Path(ningsi.__file__).resolve())
    except ImportError:
        pass

    for root in engine_src_dirs():
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            import ningsi  # noqa: F401
            return str(Path(ningsi.__file__).resolve())
        except ImportError:
            continue

    raise EngineNotFound(ENGINE_HINT)


ENGINE_PATH = ensure_engine()


def engine_versions() -> dict:
    """引擎口径版本号（报告中必须与上游一致）。"""
    from ningsi import config
    return {
        "product": config.VERSION,
        "spectrum": config.SPECTRUM_SPEC,
        "indicator": config.INDICATOR_SPEC,
        "baseline": config.BASELINE_SPEC,
        "assessment": config.LABEL_SPEC,
    }
