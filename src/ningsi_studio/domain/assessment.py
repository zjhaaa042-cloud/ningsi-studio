"""联合评估与报告：直接复用引擎的判定逻辑与报告模板，保证三端结论一致。"""

from __future__ import annotations

from ningsi import config
from ningsi.assessment.joint import assess as joint_assess
from ningsi.assessment.report import SessionReport


def eeg_summary(*, baseline, quality, indicator_summary) -> dict:
    """构造联合评估需要的脑电侧输入（缺一不可，缺失时明确标注不可用）。"""
    def mean_of(name):
        item = (indicator_summary or {}).get(name) or {}
        return item.get("mean")

    return {
        "available": bool(baseline is not None and baseline.valid),
        "valid_ratio": (quality or {}).get("valid_ratio", 0.0),
        "focus": mean_of("focus"),
        "relax": mean_of("relax"),
        "load": mean_of("load"),
        "spec": config.INDICATOR_SPEC,
        "baseline_spec": config.BASELINE_SPEC,
    }


def assess(eeg, scale_objects=None, behavior=None):
    return joint_assess(eeg, scale_objects or {}, behavior or {})


def compose_report(*, participant: str, session: str, run: str, indicators: dict,
                   quality: dict, scales: dict, behavior: dict, assessment,
                   extras: dict | None = None) -> SessionReport:
    return SessionReport(
        participant=participant,
        session=session,
        run=run,
        indicators=indicators,
        quality=quality,
        scales=scales,
        behavior=behavior,
        assessment=assessment,
        extras=extras or {},
    )


def write_report(report: SessionReport, reports_dir, stem: str) -> dict:
    """写 Markdown 与 JSON 两份报告，返回路径字典。"""
    from pathlib import Path

    target = Path(reports_dir)
    target.mkdir(parents=True, exist_ok=True)
    md_path = target / f"{stem}_report.md"
    json_path = target / f"{stem}_report.json"
    md_path.write_text(report.to_markdown(), encoding="utf-8")
    json_path.write_text(report.to_json(), encoding="utf-8")
    return {"report_md": md_path, "report_json": json_path}
