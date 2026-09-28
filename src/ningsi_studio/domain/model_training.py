"""模型训练：六维频带特征 + 逻辑回归 + 被试级 6:2:2 划分（复用上游实现）。

训练数据默认来自仿真被试（可复现、无隐私问题）；若工作区有成规模的
Dataset Studio 导出，可通过 `windows_from_records` 换成真实特征。
"""

from __future__ import annotations

from ningsi import config
from ningsi.acquisition.simulate import SyntheticEEG
from ningsi.models import logistic as logistic_module
from ningsi.signal.window import analyze_window

FEATURE_NAMES = tuple(logistic_module.FEATURE_NAMES)


def _matrix(rows, indices):
    import numpy as np
    return np.array([rows[index] for index in indices], dtype=float)


def _labels(labels, indices):
    import numpy as np
    return np.array([labels[index] for index in indices], dtype=int)


def train(*, subjects: int = 6, windows_per_state: int = 8, srate: float = 250.0,
          channels: int = 1, seed: int = 7):
    """训练并评估一个可解释基线模型，返回 (模型, 指标字典)。"""
    matrix: list[list[float]] = []
    labels: list[int] = []
    participants: list[str] = []
    for index in range(max(2, int(subjects))):
        sim = SyntheticEEG(srate=srate, channels=channels, seed=int(seed) + index)
        for state, label in (("focused", 1), ("drowsy", 0)):
            for _ in range(max(1, int(windows_per_state))):
                window = analyze_window(sim.window(state), srate)
                row = logistic_module.features_from_window(window)
                matrix.append([row[name] for name in FEATURE_NAMES])
                labels.append(label)
                participants.append(f"sim{index:02d}")

    split = logistic_module.subject_split(participants, seed=int(seed))
    index_sets = {
        "train": [i for i, name in enumerate(participants) if name in split["train"]],
        "validation": [i for i, name in enumerate(participants) if name in split["validation"]],
        "test": [i for i, name in enumerate(participants) if name in split["test"]],
    }
    if not index_sets["train"]:
        index_sets["train"] = list(range(len(matrix)))

    model = logistic_module.fit_logistic(
        _matrix(matrix, index_sets["train"]), _labels(labels, index_sets["train"]),
        trained_subjects=tuple(split["train"] or participants),
    )
    metrics = {
        "spec": model.spec,
        "version": config.VERSION,
        "features": list(FEATURE_NAMES),
        "subject_split": split,
        "samples": len(matrix),
        "participants": sorted(set(participants)),
        "train": logistic_module.evaluate(model, _matrix(matrix, index_sets["train"]),
                                          _labels(labels, index_sets["train"])),
        "validation": (logistic_module.evaluate(model, _matrix(matrix, index_sets["validation"]),
                                                _labels(labels, index_sets["validation"]))
                       if index_sets["validation"] else None),
        "test": (logistic_module.evaluate(model, _matrix(matrix, index_sets["test"]),
                                          _labels(labels, index_sets["test"]))
                 if index_sets["test"] else None),
    }
    return model, metrics


def train_and_save(model_path, *, artifact_root=None, **kwargs) -> dict:
    """训练并把模型写到 `model_path`，返回指标（含实际落盘路径）。

    `artifact_root` 仅用于把模型放进某次会话的产物目录；传入时优先使用该目录。
    """
    from pathlib import Path

    target = Path(artifact_root) / "models" / "classifier.json" if artifact_root else Path(model_path)
    model, metrics = train(**kwargs)
    path = model.save(target)
    metrics["model_path"] = str(path)
    return metrics
